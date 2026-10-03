"""The recognizers with fake models: routing, cleanup, and the in-memory path. No weights are loaded."""
from types import SimpleNamespace
import threading
import time
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
    with pytest.raises(transcription.TranscriptionError, match="stalled"):
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
    assert transcriber.transcribe_pcm(b"\x01\x00" * 1600) == "spoken words."


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
