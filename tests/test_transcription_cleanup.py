"""The recognizers with fake models: routing, cleanup, and the in-memory path. No weights are loaded."""
from types import SimpleNamespace
import threading
import time

import pytest

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
    with pytest.raises(transcription.TranscriptionError, match="stalled"):
        stranded._final_transcribe_pcm(b"\x01\x02")


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


def test_media_converter_start_failure_removes_temporary_audio(tmp_path, monkeypatch):
    (tmp_path / "example.mp3").write_bytes(b"synthetic input")
    monkeypatch.setattr(transcription.tempfile, "tempdir", str(tmp_path))
    monkeypatch.setattr(transcription, "resolve_ffmpeg", lambda: "/missing/ffmpeg")

    def fail(*_args, **_kwargs):
        raise FileNotFoundError("converter missing")

    monkeypatch.setattr(transcription.subprocess, "run", fail)
    with pytest.raises(transcription.TranscriptionError, match="Could not start media conversion"):
        transcription.normalize_media(tmp_path / "example.mp3")
    assert not list(tmp_path.glob("*.wav"))


def test_missing_ffmpeg_leaves_no_temporary_file(tmp_path, monkeypatch):
    (tmp_path / "example.mp3").write_bytes(b"synthetic input")
    monkeypatch.setattr(transcription.tempfile, "tempdir", str(tmp_path))
    monkeypatch.setenv("PATH", str(tmp_path))
    monkeypatch.setattr(transcription, "RUNTIME_BIN_CANDIDATES", ("bin",))
    with pytest.raises(transcription.TranscriptionError, match="ffmpeg is required"):
        transcription.normalize_media(tmp_path / "example.mp3")
    assert not list(tmp_path.glob("*.wav"))


def test_complete_cached_model_is_loaded_as_a_local_directory(tmp_path, monkeypatch):
    (tmp_path / "config.json").write_text("{}")
    (tmp_path / "model.safetensors").write_bytes(b"synthetic weights")
    monkeypatch.setattr(transcription, "try_to_load_from_cache", lambda _model, name: str(tmp_path / name))
    assert transcription.cached_model_source("test/model") == str(tmp_path)
    (tmp_path / "model.safetensors").unlink()
    assert transcription.cached_model_source("test/model") == "test/model"


def test_cache_files_from_different_revisions_are_not_mixed(tmp_path, monkeypatch):
    first, second = tmp_path / "one", tmp_path / "two"
    first.mkdir()
    second.mkdir()
    (first / "config.json").write_text("{}")
    (second / "model.safetensors").write_bytes(b"synthetic weights")
    monkeypatch.setattr(transcription, "try_to_load_from_cache", lambda _model, name:
                        str((first if name == "config.json" else second) / name))
    assert transcription.cached_model_source("test/model") == "test/model"


def recognizer(calls):
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
        generate=lambda mel: calls.append(mel) or [SimpleNamespace(text=" spoken words ", sentences=[], tokens=[])],
    )
    return transcriber


def test_dictation_is_recognized_from_memory_without_a_temporary_file(monkeypatch, tmp_path):
    calls = []
    monkeypatch.setattr(transcription, "get_logmel", lambda audio, config: len(audio))
    monkeypatch.setattr(transcription.tempfile, "tempdir", str(tmp_path))
    progress = []
    text = recognizer(calls).transcribe_pcm(b"\x01\x00" * 16000, progress_callback=lambda *a: progress.append(a))
    assert text == "spoken words"
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
    assert recognizer(calls).transcribe_pcm(b"\x01\x00" * 1600 + b"\x07") == "spoken words"
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
    assert transcriber.start_drafts(lambda: [b"\x01\x00" * 16000], drafts.append)
    deadline = time.monotonic() + 2
    while not drafts and time.monotonic() < deadline:
        time.sleep(0.01)
    assert drafts and drafts[0] == "draft"
    assert not transcriber.start_drafts(lambda: [], drafts.append)  # Never two streams on one encoder.
    assert not transcriber.finish_drafts()
    with pytest.raises(transcription.TranscriptionError, match="stalled"):
        transcriber.transcribe_pcm(b"\x01\x00" * 1600)
    assert calls == []
    before = time.monotonic()
    assert not transcriber.finish_drafts()   # Known to be wedged: looked at again, not waited for.
    assert time.monotonic() - before < 0.1
    release.set()
    transcriber._drafts.join(timeout=2)
    assert transcriber.finish_drafts()
    assert transcriber.transcribe_pcm(b"\x01\x00" * 1600) == "spoken words"


def test_high_accuracy_pass_receives_the_users_vocabulary():
    seen = {}
    model = transcription.QwenTranscriber()
    model.model = SimpleNamespace(transcribe=lambda samples, **kwargs: seen.update(kwargs) or SimpleNamespace(text="ok"))
    assert model.transcribe_pcm(b"\x01\x02" * 100, context="Vocabulary: Maramax.") == "ok"
    assert seen == {"language": "en", "context": "Vocabulary: Maramax."}


@pytest.mark.parametrize("heard, expected", [
    ("Vocabulary: Maramax, Cairos.", ""),            # The model repeated its context: not a transcript.
    ("vocabulary maramax", ""),                      # A partial echo.
    ("Maramax, Cairos.", ""),                        # The term list without its label.
    ("Maramax is ready.", "Maramax is ready."),      # Real speech that uses a vocabulary word.
    ("Okay.", "Okay."),
])
def test_vocabulary_echoed_back_on_silence_is_not_a_transcript(heard, expected):
    model = transcription.QwenTranscriber()
    model.model = SimpleNamespace(transcribe=lambda samples, **kwargs: SimpleNamespace(text=heard))
    assert model.transcribe_pcm(b"\x01\x02" * 100, context="Vocabulary: Maramax, Cairos.") == expected
    assert model.transcribe_pcm(b"\x01\x02" * 100) == heard  # No context, nothing to echo.
