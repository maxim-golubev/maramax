"""The recognizers with fake models: routing, cleanup, and the in-memory path. No weights are loaded."""
import re
import sys
from types import SimpleNamespace
import threading
import time
import wave
import weakref
import zlib

import numpy as np
import pytest
from parakeet_mlx.alignment import AlignedToken

from parakeet_dictation import transcription
from parakeet_dictation.app import DictationApp
from parakeet_dictation.config import AppConfig


def routing(high_accuracy=True, qwen_ready=True, encoder_free=True):
    controller = object.__new__(DictationApp)
    controller.config = AppConfig(high_accuracy=high_accuracy)
    controller._cancel_event = threading.Event()
    controller.qwen = SimpleNamespace(is_ready=lambda: qwen_ready, transcribe_pcm=lambda pcm, context=None: "")
    controller.transcriber = SimpleNamespace(
        finish_drafts=lambda: encoder_free,
        transcribe_pcm=lambda pcm, progress_callback=None: "Standard words",
    )
    return controller


def test_model_cache_is_released_when_inference_fails(monkeypatch):
    calls = []

    def fail(_mel):
        raise transcription.TranscriptionError("inference failed")

    transcriber = recognizer([])
    transcriber.model.generate = fail
    monkeypatch.setattr(transcription, "get_logmel", lambda audio, config: audio)
    monkeypatch.setattr(transcription.mx, "clear_cache", lambda: calls.append("clear"))
    monkeypatch.setattr(transcription.gc, "collect", lambda: calls.append("collect"))
    with pytest.raises(transcription.TranscriptionError, match="inference failed"):
        transcriber.transcribe_pcm(b"\x01\x00" * 1600)
    assert calls == ["collect", "clear"]


def test_a_dictation_comes_out_written_as_a_person_writes(monkeypatch):
    transcriber = recognizer([], generate=lambda mel: [recognized("Um,", "meet", "at", "8.45", "p.m.")])
    monkeypatch.setattr(transcription, "get_logmel", lambda audio, config: audio)
    assert transcriber.transcribe_pcm(b"\x01\x00" * 1600) == "Meet at 8:45 p.m."


def test_the_capture_is_out_of_memory_before_the_cache_is_cleared(monkeypatch):
    held, alive = [], []
    transcriber = recognizer([], generate=lambda mel: [recognized("spoken", "words.")])
    monkeypatch.setattr(transcription, "get_logmel", lambda audio, config: audio)

    def first_pass_only(audio, tokens, progress_callback):
        held.append(weakref.ref(audio))
        return tokens

    transcriber._repair_collapses = first_pass_only
    monkeypatch.setattr(transcription.mx, "clear_cache", lambda: alive.append(held[0]() is not None))
    assert transcriber.transcribe_pcm(b"\x01\x00" * 1600) == "spoken words."
    assert alive == [False]  # Otherwise its buffer waits in the cache until the next inference.


def test_empty_high_accuracy_result_falls_back_without_loading_another_model():
    assert routing()._final_transcribe_pcm(b"\x01\x02") == "Standard words"


def test_standard_engine_is_used_alone_when_high_accuracy_is_off():
    controller = routing(high_accuracy=False)
    controller.qwen.transcribe_pcm = lambda *_a, **_k: pytest.fail("Qwen must not run")
    assert controller._final_transcribe_pcm(b"\x01\x02") == "Standard words"


def test_cancel_before_inference_does_not_enter_a_model():
    controller = routing()
    controller._cancel_event.set()
    controller.qwen.transcribe_pcm = lambda *_a, **_k: pytest.fail("no model after cancel")
    with pytest.raises(transcription.TranscriptionError, match="Cancelled"):
        controller._final_transcribe_pcm(b"\x01\x02")


def test_cancel_during_failed_high_accuracy_pass_does_not_start_fallback():
    controller = routing()

    def fail(_pcm, context=None):
        controller._cancel_event.set()
        raise RuntimeError("failed after cancellation")

    controller.qwen.transcribe_pcm = fail
    with pytest.raises(transcription.TranscriptionError, match="Cancelled"):
        controller._final_transcribe_pcm(b"\x01\x02")


def test_stuck_draft_stream_is_rescued_by_the_other_engine_or_reported():
    rescued = routing(high_accuracy=False, encoder_free=False)
    rescued.qwen.transcribe_pcm = lambda pcm, context=None: "Rescued words"
    rescued.transcriber.transcribe_pcm = lambda *_a, **_k: pytest.fail("the stuck encoder must not be used")
    assert rescued._final_transcribe_pcm(b"\x01\x02") == "Rescued words"
    stranded = routing(qwen_ready=False, encoder_free=False)
    stranded.transcriber.transcribe_pcm = lambda *_a, **_k: pytest.fail("the stuck encoder must not be used")
    with pytest.raises(transcription.TranscriptionError, match=f"^{re.escape(transcription.ENGINE_STALLED)}$"):
        stranded._final_transcribe_pcm(b"\x01\x02")


def test_vocabulary_alone_stands_only_if_the_standard_engine_also_hears_speech():
    controller = routing()
    controller.config.replacements = [{"heard": "mara max", "replacement": "Maramax"}]
    controller.qwen.transcribe_pcm = lambda pcm, context=None: "Maramax."
    assert controller._final_transcribe_pcm(b"\x01\x02") == "Maramax."    # The user said it.
    controller.transcriber.transcribe_pcm = lambda *_a, **_k: ""
    assert controller._final_transcribe_pcm(b"\x01\x02") == ""            # Context repeated over no speech.
    controller.qwen.transcribe_pcm = lambda pcm, context=None: "Maramax is ready."
    controller.transcriber.transcribe_pcm = lambda *_a, **_k: pytest.fail("a sentence needs no second opinion")
    assert controller._final_transcribe_pcm(b"\x01\x02") == "Maramax is ready."


def test_an_echo_is_never_reported_as_a_stalled_engine():
    controller = routing(high_accuracy=False, encoder_free=False)
    controller.config.replacements = [{"heard": "mara max", "replacement": "Maramax"}]
    controller.transcriber.transcribe_pcm = lambda *_a, **_k: pytest.fail("the stuck encoder must not be used")
    controller.qwen.transcribe_pcm = lambda pcm, context=None: ""            # An echo, already discarded.
    assert controller._final_transcribe_pcm(b"\x01\x02") == ""
    controller.qwen.transcribe_pcm = lambda pcm, context=None: "Maramax."    # Nothing can check it: it stands.
    assert controller._final_transcribe_pcm(b"\x01\x02") == "Maramax."


def test_high_accuracy_pass_is_given_the_users_vocabulary():
    controller = routing()
    controller.config.replacements = [{"heard": "mara max", "replacement": "Maramax"}]
    seen = []
    controller.qwen.transcribe_pcm = lambda pcm, context=None: seen.append(context) or "ok"
    assert controller._final_transcribe_pcm(b"\x01\x02") == "ok"
    controller.config.use_corrections = False
    controller._final_transcribe_pcm(b"\x01\x02")
    assert seen == ["Vocabulary: Maramax.", None]


def test_failed_model_download_can_be_retried_without_duplicate_loads(monkeypatch):
    attempts = []
    release = threading.Event()

    def load(_model_id):
        attempts.append(True)
        if len(attempts) == 1:
            raise OSError("offline")
        assert release.wait(timeout=2)
        return SimpleNamespace()

    monkeypatch.setattr(transcription, "from_pretrained", load)
    monkeypatch.setattr(transcription, "model_folder", lambda model: model.repo)
    monkeypatch.setattr(transcription.ParakeetTranscriber, "_warm_model", lambda self: None)
    transcriber = transcription.ParakeetTranscriber()
    assert transcriber.ready_event.wait(timeout=2)
    assert "unavailable" in transcriber.status_message()
    with pytest.raises(transcription.TranscriptionError, match="offline"):
        transcriber.wait_until_ready()
    try:
        assert transcriber.retry_loading()
        assert "Preparing" in transcriber.status_message()
        assert not transcriber.retry_loading()
    finally:
        release.set()
    assert transcriber.ready_event.wait(timeout=2)
    transcriber.wait_until_ready()
    assert transcriber.is_ready() and transcriber.status_message() == "Speech model ready"
    assert not transcriber.retry_loading()
    assert len(attempts) == 2


def test_high_accuracy_failure_releases_inference_and_cache(monkeypatch):
    def fail(*args, **kwargs):
        raise RuntimeError("inference failed")

    calls = []
    model = transcription.QwenTranscriber()
    model.model = SimpleNamespace(transcribe=fail)
    monkeypatch.setattr(transcription.mx, "clear_cache", lambda: calls.append("clear"))
    with pytest.raises(RuntimeError, match="inference failed"):
        model.transcribe_pcm(b"\x01\x02" * 100)
    assert model._active_inferences == 0
    assert calls == ["clear"]


def fake_qwen_library(monkeypatch, load):
    """The high-accuracy model library, its from_pretrained replaced by `load`."""
    monkeypatch.setitem(sys.modules, "qwen3_asr_mlx", SimpleNamespace(
        Qwen3ASR=SimpleNamespace(from_pretrained=load)))
    monkeypatch.setattr(transcription, "model_folder", lambda model: model.repo)


def wait_for(condition):
    deadline = time.monotonic() + 2
    while not condition() and time.monotonic() < deadline:
        time.sleep(0.01)
    return condition()


def test_high_accuracy_turned_back_on_while_a_discarded_model_closes_is_loaded(monkeypatch):
    loading, closing, closed = threading.Event(), threading.Event(), threading.Event()
    loads = []

    class Model:
        def warm_up(self):
            pass

        def close(self):
            if len(loads) == 1:
                closing.set()
                assert closed.wait(timeout=2)  # Releasing the weights takes a moment.

    def load(_source):
        loads.append(Model())
        if len(loads) == 1:
            assert loading.wait(timeout=2)
        return loads[-1]

    fake_qwen_library(monkeypatch, load)
    qwen = transcription.QwenTranscriber()
    qwen.start_loading()            # On,
    qwen.unload()                   # off while it loads,
    loading.set()
    assert closing.wait(timeout=2)  # so the loaded model is closed,
    qwen.start_loading()            # and on again during that close.
    closed.set()
    assert wait_for(qwen.is_ready)
    assert qwen.model is loads[1] and qwen.status_message() == "High-accuracy model ready"


def test_a_discarded_model_that_fails_to_close_is_not_a_failed_load(monkeypatch):
    failures = []
    loaded = threading.Event()

    class Model:
        def warm_up(self):
            pass

        def close(self):
            loaded.set()
            raise RuntimeError("close failed")

    gate = threading.Event()

    def load(_source):
        assert gate.wait(timeout=2)
        return Model()

    fake_qwen_library(monkeypatch, load)
    monkeypatch.setattr(transcription.logger, "warning", lambda _message: None)
    qwen = transcription.QwenTranscriber(on_load_failed=failures.append)
    qwen.start_loading()
    qwen.unload()
    gate.set()
    assert loaded.wait(timeout=2)
    assert wait_for(lambda: not any(thread.name.endswith("(_load)") for thread in threading.enumerate()))
    assert qwen.load_error is None and failures == [] and not qwen.is_ready()


def test_a_failed_high_accuracy_load_is_logged_with_its_traceback(monkeypatch):
    def load(_source):
        raise KeyError("encoder")

    logged, failures = [], []
    fake_qwen_library(monkeypatch, load)
    monkeypatch.setattr(transcription.logger, "exception", logged.append)
    qwen = transcription.QwenTranscriber(on_load_failed=failures.append)
    qwen.start_loading()
    assert wait_for(lambda: failures)
    assert logged == [f"Could not load the high-accuracy model {qwen.MODEL_ID}"]
    assert isinstance(qwen.load_error, KeyError) and not qwen._loading
    assert qwen.status_message().startswith("High-accuracy model could not be loaded")


def test_a_failed_warm_up_is_logged_with_its_traceback_and_frees_the_cache(monkeypatch):
    calls = []

    def warm_up_fails(self):
        raise AttributeError("no attribute 'encoder'")

    monkeypatch.setattr(transcription, "from_pretrained", lambda _source: SimpleNamespace())
    monkeypatch.setattr(transcription, "model_folder", lambda model: model.repo)
    monkeypatch.setattr(transcription.ParakeetTranscriber, "_warm_model", warm_up_fails)
    monkeypatch.setattr(transcription.logger, "exception", lambda message: calls.append(message))
    monkeypatch.setattr(transcription.gc, "collect", lambda: calls.append("collect"))
    monkeypatch.setattr(transcription.mx, "clear_cache", lambda: calls.append("clear"))
    transcriber = transcription.ParakeetTranscriber(transcription.ModelFiles("test/parakeet", "0" * 40, ("config.json",)))
    assert transcriber.ready_event.wait(timeout=2)
    assert transcriber.model is None and isinstance(transcriber.load_error, AttributeError)
    assert calls == ["Could not load the Parakeet model test/parakeet", "collect", "clear"]


def media(tmp_path, monkeypatch, run):
    """An imported file, converted by `run` in place of FFmpeg, with its
    temporary WAV created in `tmp_path`."""
    (tmp_path / "example.mp3").write_bytes(b"synthetic input")
    monkeypatch.setattr(transcription.tempfile, "tempdir", str(tmp_path))
    monkeypatch.setattr(transcription, "resolve_ffmpeg", lambda: "/usr/local/bin/ffmpeg")
    monkeypatch.setattr(transcription.subprocess, "run", run)
    return tmp_path / "example.mp3"


def test_media_converter_start_failure_removes_temporary_audio(tmp_path, monkeypatch):
    def fail(*_args, **_kwargs):
        raise FileNotFoundError("converter missing")

    with pytest.raises(transcription.TranscriptionError, match="^FFmpeg did not start$"):
        with transcription.converted_media(media(tmp_path, monkeypatch, fail)):
            pytest.fail("nothing was converted")
    assert not list(tmp_path.glob("*.wav"))


def test_missing_ffmpeg_leaves_no_temporary_file(tmp_path, monkeypatch):
    (tmp_path / "example.mp3").write_bytes(b"synthetic input")
    monkeypatch.setattr(transcription.tempfile, "tempdir", str(tmp_path))
    monkeypatch.setenv("PATH", str(tmp_path))
    monkeypatch.setattr(transcription, "RUNTIME_BIN_CANDIDATES", ("bin",))
    with pytest.raises(transcription.TranscriptionError,
                       match="^Importing media needs FFmpeg — install it with brew install ffmpeg$"):
        with transcription.converted_media(tmp_path / "example.mp3"):
            pytest.fail("nothing was converted")
    assert not list(tmp_path.glob("*.wav"))


def test_converted_media_exists_only_inside_the_with(tmp_path, monkeypatch):
    def convert(command, **_kwargs):
        with wave.open(command[-1], "wb") as audio:
            audio.setnchannels(1)
            audio.setsampwidth(2)
            audio.setframerate(16000)
            audio.writeframes(b"\x01\x00" * 160)
        return SimpleNamespace(returncode=0, stderr="")

    with transcription.converted_media(media(tmp_path, monkeypatch, convert)) as converted:
        with wave.open(converted, "rb") as audio:
            assert audio.getnframes() == 160
    assert not list(tmp_path.glob("*.wav"))


def unreadable(*_args, **_kwargs):
    return SimpleNamespace(returncode=1, stderr="example.mp3: Invalid data found when processing input")


def too_slow(*_args, **_kwargs):
    raise transcription.subprocess.TimeoutExpired("ffmpeg", transcription.FFMPEG_TIMEOUT_SECONDS)


@pytest.mark.parametrize("run, message", [
    (unreadable, "FFmpeg could not read the file"),
    (too_slow, "FFmpeg took over 120 s to convert the file"),
])
def test_a_failed_conversion_says_why_whatever_number_of_files_failed(tmp_path, monkeypatch, run, message):
    """Read beside the file's name in the queue, and in a run's summary of several files."""
    monkeypatch.setattr(transcription.logger, "error", lambda _message: None)
    with pytest.raises(transcription.TranscriptionError) as failure:
        with transcription.converted_media(media(tmp_path, monkeypatch, run)):
            pytest.fail("nothing was converted")
    assert str(failure.value) == message
    assert not list(tmp_path.glob("*.wav"))


def test_a_converted_copy_that_cannot_be_deleted_is_reported_not_raised(tmp_path, monkeypatch):
    warned = []

    def refuse(self, missing_ok=False):
        raise PermissionError(f"cannot delete {self}")

    path = media(tmp_path, monkeypatch, lambda *_a, **_k: SimpleNamespace(returncode=0, stderr=""))
    monkeypatch.setattr(transcription.logger, "warning", warned.append)
    monkeypatch.setattr(transcription.Path, "unlink", refuse)
    with transcription.converted_media(path) as converted:
        pass
    assert len(warned) == 1 and converted in warned[0] and "example.mp3" in warned[0]


TEST_MODEL = transcription.ModelFiles("test/model", "0123456789abcdef", ("config.json", "model.safetensors"))


def downloads(monkeypatch, outcome=None):
    """What model_folder() asks Hugging Face for; `outcome` is raised, if given."""
    asked = []

    def download(repo, allow_patterns, revision=None):
        asked.append((repo, revision, allow_patterns))
        if outcome is not None:
            raise outcome
        return "/downloaded"

    monkeypatch.setattr(transcription, "snapshot_download", download)
    return asked


def test_complete_cached_model_is_loaded_as_a_local_directory_without_a_request(tmp_path, monkeypatch):
    (tmp_path / "config.json").write_text("{}")
    (tmp_path / "model.safetensors").write_bytes(b"synthetic weights")
    monkeypatch.setattr(transcription, "try_to_load_from_cache",
                        lambda _repo, name, revision: str(tmp_path / name))
    asked = downloads(monkeypatch)
    assert transcription.model_folder(TEST_MODEL) == str(tmp_path) and asked == []
    (tmp_path / "model.safetensors").unlink()                   # A download cut short: fetched again,
    assert transcription.model_folder(TEST_MODEL) == "/downloaded"
    assert asked == [("test/model", "0123456789abcdef", ["config.json", "model.safetensors"])]  # as tested.


def test_cache_files_from_different_revisions_are_not_mixed(tmp_path, monkeypatch):
    first, second = tmp_path / "one", tmp_path / "two"
    first.mkdir()
    second.mkdir()
    (first / "config.json").write_text("{}")
    (second / "model.safetensors").write_bytes(b"synthetic weights")
    monkeypatch.setattr(transcription, "try_to_load_from_cache", lambda _repo, name, revision:
                        str((first if name == "config.json" else second) / name))
    downloads(monkeypatch)
    assert transcription.model_folder(TEST_MODEL) == "/downloaded"


def test_the_release_commit_is_found_in_the_cache_when_the_latest_is_not_complete(tmp_path, monkeypatch):
    for name in TEST_MODEL.files:
        (tmp_path / name).write_text("x")
    monkeypatch.setattr(transcription, "try_to_load_from_cache", lambda _repo, name, revision:
                        str(tmp_path / name) if revision == TEST_MODEL.revision else None)
    assert transcription.model_folder(TEST_MODEL) == str(tmp_path) and downloads(monkeypatch) == []


def test_a_model_hugging_face_no_longer_offers_is_named_as_such(monkeypatch):
    from huggingface_hub.errors import RepositoryNotFoundError
    monkeypatch.setattr(transcription, "try_to_load_from_cache", lambda *_args, **_kwargs: None)
    downloads(monkeypatch, RepositoryNotFoundError("404 Client Error", response=SimpleNamespace(
        headers={}, status_code=404, request=None)))
    with pytest.raises(transcription.ModelWithdrawn, match="no longer offers test/model"):
        transcription.model_folder(TEST_MODEL)


def test_the_models_name_the_files_their_libraries_read():
    assert set(transcription.QWEN.files) >= {"config.json", "model.safetensors", "vocab.json", "merges.txt"}
    assert len(transcription.PARAKEET.revision) == len(transcription.QWEN.revision) == 40


def recognized(*words, gap=0.5):
    """What the model returns for `words`, one token per word, `gap` apart."""
    tokens = [AlignedToken(index, f" {word}", start=index * gap, duration=gap * 0.8) for index, word in enumerate(words)]
    return SimpleNamespace(sentences=[SimpleNamespace(tokens=tokens)], tokens=tokens)


def recognizer(calls, generate=None):
    """A ParakeetTranscriber whose model records what it was asked for."""
    transcriber = object.__new__(transcription.ParakeetTranscriber)
    transcriber.ready_event = threading.Event()
    transcriber.ready_event.set()
    transcriber.load_error = None
    transcriber._drafts = None
    transcriber._drafts_stop = threading.Event()
    transcriber._drafts_wedged = False
    transcriber.model = SimpleNamespace(
        preprocessor_config=SimpleNamespace(sample_rate=16000, hop_length=160, win_length=400, n_fft=512),
        generate=generate or (lambda mel: calls.append(mel) or [recognized("spoken", "words.")]),
    )
    return transcriber


def test_dictation_is_recognized_from_memory_without_a_temporary_file(monkeypatch, tmp_path):
    calls = []
    monkeypatch.setattr(transcription, "get_logmel", lambda audio, config: len(audio))
    monkeypatch.setattr(transcription.tempfile, "tempdir", str(tmp_path))
    progress = []
    text = recognizer(calls).transcribe_pcm(b"\x01\x00" * 16000, progress_callback=lambda *a: progress.append(a))
    assert text == "spoken words."
    assert calls == [16000]
    assert progress == [(16000, 16000)]
    assert not list(tmp_path.iterdir())


def test_long_dictation_is_chunked_in_memory_with_no_ffmpeg(monkeypatch, tmp_path):
    calls = []
    monkeypatch.setattr(transcription, "get_logmel", lambda audio, config: len(audio))
    monkeypatch.setattr(transcription, "sentences_to_result", lambda sentences: SimpleNamespace(text="merged words"))
    monkeypatch.setattr(transcription, "tokens_to_sentences", lambda tokens, config: [])
    monkeypatch.setattr(transcription.tempfile, "tempdir", str(tmp_path))
    monkeypatch.setattr(transcription.subprocess, "run", lambda *a, **k: pytest.fail("no FFmpeg for a dictation"))
    progress = []
    pcm = b"\x01\x00" * (16000 * 250)
    assert recognizer(calls).transcribe_pcm(pcm, progress_callback=lambda *a: progress.append(a)) == "merged words"
    # 120 s chunks advancing by 105 s (15 s overlap): 0-120, 105-225, 210-250.
    assert calls == [16000 * 120, 16000 * 120, 16000 * 40]
    assert progress == [(16000 * 120, 16000 * 250), (16000 * 225, 16000 * 250), (16000 * 250, 16000 * 250)]
    assert not list(tmp_path.iterdir())


def test_spectrogram_input_is_padded_only_as_far_as_the_last_frame_reads(monkeypatch):
    calls = []
    monkeypatch.setattr(transcription, "get_logmel", lambda audio, config: len(audio))
    for samples, expected in ((1600, 1600), (1600 + 47, 1600 + 47), (1600 + 48, 1600 + 160), (1600 + 159, 1600 + 160)):
        recognizer(calls).transcribe_pcm(b"\x01\x00" * samples)
        assert calls[-1] == expected, samples


def test_audio_too_short_for_a_spectrogram_is_no_speech():
    calls = []
    assert recognizer(calls).transcribe_pcm(b"\x01\x00" * 100) == ""
    assert recognizer(calls).transcribe_pcm(b"") == ""
    assert calls == []


def test_odd_length_capture_is_trimmed_not_rejected(monkeypatch):
    calls = []
    monkeypatch.setattr(transcription, "get_logmel", lambda audio, config: len(audio))
    assert recognizer(calls).transcribe_pcm(b"\x01\x00" * 1600 + b"\x07") == "spoken words."
    model = transcription.QwenTranscriber()
    model.model = SimpleNamespace(transcribe=lambda samples, **kwargs: SimpleNamespace(text=str(len(samples))))
    assert model.transcribe_pcm(b"\x01\x00" * 100 + b"\x07") == "100"


def test_offline_pass_waits_for_the_draft_stream_and_refuses_if_it_is_stuck(monkeypatch):
    calls = []
    monkeypatch.setattr(transcription, "get_logmel", lambda audio, config: len(audio))
    monkeypatch.setattr(transcription, "DRAFT_RELEASE_SECONDS", 0.2)
    transcriber = recognizer(calls)
    release = threading.Event()

    class Stream:
        result = SimpleNamespace(text="draft")

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            release.wait(timeout=5)  # A stream that will not let go of the encoder.

        def add_audio(self, _audio):
            pass

    transcriber.model.transcribe_stream = lambda context_size: Stream()
    drafts = []
    assert transcriber.start_drafts(lambda: [b"\x01\x00" * 32000], drafts.append)
    deadline = time.monotonic() + 2
    while not drafts and time.monotonic() < deadline:
        time.sleep(0.01)
    assert drafts and drafts[0] == "draft"
    assert not transcriber.start_drafts(lambda: [], drafts.append)  # Never two streams on one encoder.
    assert not transcriber.finish_drafts()
    with pytest.raises(transcription.TranscriptionError, match=f"^{re.escape(transcription.ENGINE_STALLED)}$"):
        transcriber.transcribe_pcm(b"\x01\x00" * 1600)
    assert calls == []
    before = time.monotonic()
    assert not transcriber.finish_drafts()   # Known to be wedged: looked at again, not waited for.
    assert time.monotonic() - before < 0.1
    release.set()
    transcriber._drafts.join(timeout=2)
    assert transcriber.finish_drafts()
    assert transcriber.transcribe_pcm(b"\x01\x00" * 1600) == "spoken words."


def test_a_stream_that_recovers_on_its_own_does_not_make_the_next_one_look_stuck(monkeypatch):
    monkeypatch.setattr(transcription, "DRAFT_RELEASE_SECONDS", 0.2)
    transcriber = recognizer([])
    release = threading.Event()
    streams = []

    class Stream:
        result = SimpleNamespace(text="")

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            if len(streams) == 1:
                release.wait(timeout=5)  # The first stream holds the encoder for a while.

        def add_audio(self, _audio):
            pass

    transcriber.model.transcribe_stream = lambda context_size: streams.append(Stream()) or streams[-1]
    assert transcriber.start_drafts(lambda: [], lambda _text: None)
    assert not transcriber.finish_drafts()       # Found wedged.
    release.set()
    transcriber._drafts.join(timeout=2)          # It lets go by itself; nothing asks in between.
    assert transcriber.start_drafts(lambda: [], lambda _text: None)
    deadline = time.monotonic() + 2
    while len(streams) < 2 and time.monotonic() < deadline:
        time.sleep(0.01)
    assert transcriber.finish_drafts()           # The healthy stream is waited for, not judged stuck.


def words(text, start=0.0, gap=0.5):
    return [transcription.Word(start + index * gap, start + index * gap + 0.4, f" {word}")
            for index, word in enumerate(text.split())]


@pytest.mark.parametrize("text, expected", [
    ("So I think we should go. And then I said that we can do it later today.", []),
    ("so i think we should go and then we said that we", [(0.0, 5.9)]),  # 12 words, lower-case i
    ("so i think we should go and then we said that", []),              # 11 is not enough
    ("so i’m sure we should go and then we said that we", [(0.0, 5.9)]),
    (" ".join(["I think"] * 30) + " so.", []),                    # A long run-on sentence that keeps its I.
    (" ".join(["we think"] * 19) + " so.", []),                   # 39 words without punctuation or capitals
    (" ".join(["we think"] * 20) + " so.", [(0.0, 19.9)]),        # 40
    ("Done. so i think we should go and then we said that we can. Done.", [(0.5, 6.4)]),
])
def test_unformatted_stretches_are_found_and_ordinary_sentences_are_not(text, expected):
    assert transcription.collapsed_spans(words(text)) == expected


FORMATTED = [(0.0, "Hello"), (0.5, "there.")] + [
    (10.0 + index * 0.5, word) for index, word in
    enumerate("So I think we should go. And then I said that we can do it later today.".split())
] + [(30.0, "Thanks.")]


def collapse(word):
    return word.lower().strip(".,")


def timed_audio_model(calls, collapses, script=FORMATTED, stretches=((10, 20),)):
    """A model that reads where its window starts from the audio itself (each
    sample holds its position in tenths of a second) and answers with the
    words of `script` spoken in that window. `collapses(start, seconds)`
    says whether a window leaves what is spoken in `stretches` unformatted."""
    def generate(audio):
        start = round(float(audio[0]) * transcription.FULL_SCALE) / 10
        length = len(audio) / 16000
        calls.append((start, round(length, 1)))
        spoken = [(at, word) for at, word in script if start <= at < start + length]
        if collapses(start, length):
            spoken = [(at, collapse(word) if any(low <= at < high for low, high in stretches) else word)
                      for at, word in spoken]
        tokens = [AlignedToken(zlib.crc32(word.encode()), f" {word}", start=at - start, duration=0.4)
                  for at, word in spoken]
        return [SimpleNamespace(sentences=[SimpleNamespace(tokens=tokens)], tokens=tokens)]
    return generate


def timed_audio(seconds):
    return np.repeat(np.arange(seconds * 10, dtype="<i2"), 1600).tobytes()


FORTY_SECONDS = timed_audio(40)


def test_an_unformatted_stretch_is_recognized_again_and_spliced_in(monkeypatch):
    calls, progress = [], []
    monkeypatch.setattr(transcription, "get_logmel", lambda audio, config: audio)
    transcriber = recognizer(calls, timed_audio_model(calls, collapses=lambda start, seconds: seconds > 30))
    text = transcriber.transcribe_pcm(FORTY_SECONDS, progress_callback=lambda *a: progress.append(a))
    assert text == "Hello there. So I think we should go. And then I said that we can do it later today. Thanks."
    # The whole capture, then the stretch with 5 s either side (10.0-18.4 s).
    assert calls == [(0.0, 40.0), (5.0, 18.4)]
    assert progress == [(640000, 640000)] * 2  # The repair can still be cancelled.


def test_a_repair_that_changes_the_words_is_not_accepted():
    original = words("so i think we should go and then we said that we can")
    reworded = words("So, I thought we could go. And then she said that we can.")
    same = words("So I think we should go. And then we said that we can.")
    assert transcription.repair_acceptable(original, same)
    assert not transcription.repair_acceptable(original, reworded)
    assert not transcription.repair_acceptable(original, original)  # Still unformatted.


def test_word_agreement_does_not_depend_on_which_recognition_comes_first():
    original = words("for so i the then so and milk some")
    candidate = words("for so i the then so and some milk the some")
    # Nine words in common, in order: 2 x 9 / (9 + 11). A greedy match finds 8 one way round.
    assert transcription.word_agreement(original, candidate) == pytest.approx(0.9)
    assert transcription.word_agreement(candidate, original) == pytest.approx(0.9)
    assert transcription.word_agreement(words("b c"), words("c b a c")) == pytest.approx(2 / 3)
    assert transcription.word_agreement(words("c b a c"), words("b c")) == pytest.approx(2 / 3)


def timed(*spoken):
    """Tokens as the model gives them: (start, text) each, a leading space starting a word."""
    return [AlignedToken(zlib.crc32(text.encode()), text, start=at, duration=0.08) for at, text in spoken]


def text_of(tokens):
    return "".join(token.text for token in tokens).strip()


LEAD_IN = [(5.5, " finished"), (5.9, " it"), (6.2, " yet"), (6.4, ","), (6.5, " including"), (6.8, " me"),
           (7.0, "."), (7.3, " So"), (7.4, ","), (7.5, " that"), (7.6, " is"), (7.7, " on"), (7.9, " my"),
           (8.0, " list"), (8.3, ".")]
STRETCH = "so i think we should go and then i said that we can"
REDONE = "So, I think we should go. And then I said that we can."


def spoken_from(start, text):
    return [(start + index * 0.5, f" {word}") for index, word in enumerate(text.split())]


def test_a_repair_changes_only_the_stretch_even_where_the_context_was_heard_differently():
    # The first pass is formatted up to "list." and then collapses. The
    # repair window hears the lead-in as "me, so that": merged on their
    # shared words (the library's alignment pairs the two commas), that
    # read "me. So, so that", a word said once written twice.
    first_pass = timed(*LEAD_IN, *spoken_from(10.0, STRETCH), (17.0, " Thanks"), (17.3, "."))
    heard_again = timed(*LEAD_IN[:6], (7.04, ","), (7.28, " so"), *LEAD_IN[9:], *spoken_from(10.0, REDONE),
                        (17.0, " Thanks"), (17.3, "."))
    span = transcription.collapsed_spans(transcription._words(first_pass))[0]
    assert text_of(transcription.repaired(first_pass, heard_again, span, (5.0, 21.4))) == (
        "finished it yet, including me. So, that is on my list. "
        "So, I think we should go. And then I said that we can. Thanks.")


def test_a_repair_needs_the_words_either_side_of_the_stretch_heard_alike():
    first_pass = timed(*LEAD_IN, *spoken_from(10.0, STRETCH), (17.0, " Thanks"), (17.3, "."))
    misheard_edge = timed(*LEAD_IN[:-2], (8.0, " lists"), (8.3, "."), *spoken_from(10.0, REDONE),
                          (17.0, " Thanks"), (17.3, "."))
    span = transcription.collapsed_spans(transcription._words(first_pass))[0]
    assert transcription.repaired(first_pass, misheard_edge, span, (5.0, 21.4)) is None


def test_a_repair_beside_a_long_pause_is_cut_at_the_stretch():
    # "Thanks." comes 9 s after the stretch, beyond the window: it cannot be
    # heard again, and whatever the window hears in the pause is not taken.
    first_pass = timed(*LEAD_IN, *spoken_from(10.0, STRETCH), (25.0, " Thanks"), (25.3, "."))
    heard_again = timed(*LEAD_IN, *spoken_from(10.0, REDONE), (19.0, " Hm"), (19.3, "."))
    span = transcription.collapsed_spans(transcription._words(first_pass))[0]
    assert text_of(transcription.repaired(first_pass, heard_again, span, (5.0, 21.4))) == (
        "finished it yet, including me. So, that is on my list. "
        "So, I think we should go. And then I said that we can. Thanks.")


def test_every_unformatted_stretch_of_a_capture_is_repaired(monkeypatch):
    calls = []
    sentence = "So I think we should go. And then I said that we can do it later today."
    script = FORMATTED + [(40.0 + index * 0.5, word) for index, word in enumerate(sentence.split())] + [
        (60.0, "Bye.")]
    monkeypatch.setattr(transcription, "get_logmel", lambda audio, config: audio)
    transcriber = recognizer(calls, timed_audio_model(calls, lambda start, seconds: seconds > 30, script,
                                                      stretches=((10, 20), (40, 50))))
    text = transcriber.transcribe_pcm(timed_audio(70))
    assert text == f"Hello there. {sentence} Thanks. {sentence} Bye."
    assert calls == [(0.0, 70.0), (5.0, 18.4), (35.0, 18.4)]


def test_a_short_capture_is_not_recognized_again_as_its_own_window(monkeypatch):
    calls = []
    script = [(1.0 + index * 0.6, word) for index, word in
              enumerate("So I think we should go. And then I said that we can do it later today.".split())]
    monkeypatch.setattr(transcription, "get_logmel", lambda audio, config: audio)
    transcriber = recognizer(calls, timed_audio_model(calls, lambda start, seconds: True, script,
                                                      stretches=((0, 15),)))
    assert transcriber.transcribe_pcm(timed_audio(15)).startswith("so i think")
    assert calls == [(0.0, 15.0)]  # Both windows would be the whole capture again.


def test_a_window_that_collapses_too_is_retried_from_earlier(monkeypatch):
    calls = []
    monkeypatch.setattr(transcription, "get_logmel", lambda audio, config: audio)
    transcriber = recognizer(calls, timed_audio_model(calls, lambda start, seconds: seconds > 30 or start == 5.0))
    text = transcriber.transcribe_pcm(FORTY_SECONDS)
    assert text == "Hello there. So I think we should go. And then I said that we can do it later today. Thanks."
    assert calls == [(0.0, 40.0), (5.0, 18.4), (0.0, 20.0), (16.0, 7.4)]


def test_a_cancel_during_the_repair_keeps_the_finished_first_pass(monkeypatch):
    calls, progress = [], []
    monkeypatch.setattr(transcription, "get_logmel", lambda audio, config: audio)
    transcriber = recognizer(calls, timed_audio_model(calls, collapses=lambda start, seconds: seconds > 30))

    def cancel_after_first_pass(*position):
        progress.append(position)
        if len(progress) > 1:
            raise transcription.TranscriptionCancelled("Cancelled")

    text = transcriber.transcribe_pcm(FORTY_SECONDS, progress_callback=cancel_after_first_pass)
    assert text == "Hello there. so i think we should go and then i said that we can do it later today Thanks."
    assert calls == [(0.0, 40.0)]


def test_no_chunk_lies_wholly_inside_the_previous_ones_overlap(monkeypatch):
    calls = []
    monkeypatch.setattr(transcription, "get_logmel", lambda audio, config: len(audio))
    # 220 s: 0-120, then 105-220 reaches the end. The library's loop would
    # add 210-220, wholly inside that chunk's overlap.
    recognizer(calls).transcribe_pcm(b"\x01\x00" * (16000 * 220))
    assert calls == [16000 * 120, 16000 * 115]


def test_a_retry_starts_earlier_than_the_first_try():
    assert transcription.REPAIR_RETRY_LEAD_SECONDS > transcription.REPAIR_CONTEXT_SECONDS


def test_a_stretch_that_stays_unformatted_is_left_as_it_was(monkeypatch):
    calls = []
    monkeypatch.setattr(transcription, "get_logmel", lambda audio, config: audio)
    transcriber = recognizer(calls, timed_audio_model(calls, collapses=lambda start, seconds: True))
    text = transcriber.transcribe_pcm(FORTY_SECONDS)
    assert text == "Hello there. so i think we should go and then i said that we can do it later today Thanks."
    # One window, then one starting half a repair chunk earlier (two 20 s
    # chunks overlapping by 4 s), then it gives up.
    assert calls == [(0.0, 40.0), (5.0, 18.4), (0.0, 20.0), (16.0, 7.4)]


def test_high_accuracy_pass_receives_the_users_vocabulary():
    seen = {}
    model = transcription.QwenTranscriber()
    model.model = SimpleNamespace(transcribe=lambda samples, **kwargs: seen.update(kwargs) or SimpleNamespace(text="ok"))
    assert model.transcribe_pcm(b"\x01\x02" * 100, context="Vocabulary: Maramax.") == "ok"
    assert seen == {"language": "en", "context": "Vocabulary: Maramax."}


@pytest.mark.parametrize("heard, expected", [
    ("Vocabulary: Maramax, Cairos.", ""),            # The model repeated its context: not a transcript.
    ("vocabulary maramax", ""),                      # A partial echo.
    ("Vocabulary: Maramax, Cairos. Okay.", ""),      # The context and more.
    ("Maramax, Cairos.", "Maramax, Cairos."),        # The terms alone: the caller asks the audio.
    ("Maramax is ready.", "Maramax is ready."),      # Real speech that uses a vocabulary word.
    ("Okay.", "Okay."),
])
def test_vocabulary_echoed_back_on_silence_is_not_a_transcript(heard, expected):
    model = transcription.QwenTranscriber()
    model.model = SimpleNamespace(transcribe=lambda samples, **kwargs: SimpleNamespace(text=heard))
    assert model.transcribe_pcm(b"\x01\x02" * 100, context="Vocabulary: Maramax, Cairos.") == expected
    assert model.transcribe_pcm(b"\x01\x02" * 100) == heard  # No context, nothing to echo.


@pytest.mark.parametrize("heard, context, echo", [
    ("Vocabulary: Maramax.", "Vocabulary: Maramax.", transcription.Echo.CERTAIN),
    ("Vocabulary", "Vocabulary: Maramax.", transcription.Echo.CERTAIN),
    ("Maramax.", "Vocabulary: Maramax.", transcription.Echo.POSSIBLE),        # Said, or the list repeated.
    ("Maramax, Cairos.", "Vocabulary: Maramax, Cairos.", transcription.Echo.POSSIBLE),
    ("Maramax is ready.", "Vocabulary: Maramax.", transcription.Echo.NONE),
    ("Maramax, Cairos and the rest are fine.", "Vocabulary: Maramax, Cairos.", transcription.Echo.NONE),
    ("Cairos.", "Vocabulary: Maramax, Cairos.", transcription.Echo.NONE),     # Not where an echo begins.
])
def test_only_text_that_cannot_be_speech_is_a_certain_echo(heard, context, echo):
    assert transcription.context_echo(heard, context) is echo


def test_the_echo_filter_reads_the_hint_that_corrections_writes():
    # The two modules share the hint's wording; this keeps them in step.
    from parakeet_dictation.corrections import vocabulary_hint
    hint = vocabulary_hint([{"heard": "mara max", "replacement": "Maramax"},
                            {"heard": "par a keet", "replacement": "Parakeet"}])
    assert transcription.context_echo(hint, hint) is transcription.Echo.CERTAIN
    assert transcription.context_echo("Maramax, Parakeet.", hint) is transcription.Echo.POSSIBLE
    assert transcription.context_echo("Maramax is ready.", hint) is transcription.Echo.NONE


def test_the_high_accuracy_model_loads_only_once_the_standard_one_is_ready(monkeypatch):
    """Two large loads at once contend, and without the standard model
    nothing is dictated at all."""
    from parakeet_dictation import app as module

    loads, refreshed = [], []
    controller = object.__new__(DictationApp)
    controller.config = AppConfig(high_accuracy=False)
    controller.qwen = SimpleNamespace(start_loading=lambda: loads.append("qwen"), status_message=lambda: "Loading…")
    controller._save_settings = lambda: True
    controller._show_settings_changed = lambda: None
    controller._push_status = lambda message, revert_after=0: None
    controller._prepare_recorder = lambda: None
    controller._hotkey_error_message = None
    controller._leftover_found = False
    monkeypatch.setattr(module.AppHelper, "callAfter", lambda function, *args: refreshed.append(function))
    ready = [False]
    controller.transcriber = SimpleNamespace(is_ready=lambda: ready[0], status_message=lambda: "Model failed")

    controller.toggle_setting("high_accuracy")      # Turned on while Parakeet is still loading.
    assert loads == []

    def fails():
        raise transcription.TranscriptionError("download failed")
    controller.transcriber.wait_until_ready = fails
    controller._wait_for_model_readiness()
    assert loads == [] and refreshed                 # Settings still hears about it.

    ready[0] = True
    controller.transcriber.wait_until_ready = lambda: None
    controller._wait_for_model_readiness()           # Ready (or ready after Retry Speech Model).
    assert loads == ["qwen"]



def test_the_high_accuracy_model_says_it_waits_until_it_is_asked_to_load():
    """It loads only once the standard model is ready; until then it is not "loading"."""
    qwen = transcription.QwenTranscriber()
    assert qwen.status_message() == "The high-accuracy model loads once the standard model is ready"


def test_audio_shorter_than_half_an_fft_is_not_recognized(monkeypatch):
    """The library pads less than half an FFT by less, and its last frame would read past the buffer."""
    calls = []
    transcriber = recognizer(calls)
    monkeypatch.setattr(transcription, "get_logmel", lambda audio, config: audio)
    assert transcriber.transcribe_pcm(b"\x01\x00" * 256) == ""
    assert calls == []
    assert transcriber.transcribe_pcm(b"\x01\x00" * 257) and calls


def test_a_release_commit_gone_from_a_repository_still_there_falls_back_to_its_latest(monkeypatch):
    from huggingface_hub.errors import RevisionNotFoundError
    monkeypatch.setattr(transcription, "try_to_load_from_cache", lambda *_args, **_kwargs: None)
    asked = []

    def download(repo, allow_patterns, revision=None):
        asked.append(revision)
        if revision is not None:
            raise RevisionNotFoundError("404", response=SimpleNamespace(headers={}, status_code=404, request=None))
        return "/latest"

    monkeypatch.setattr(transcription, "snapshot_download", download)
    assert transcription.model_folder(TEST_MODEL) == "/latest" and asked == [TEST_MODEL.revision, None]

