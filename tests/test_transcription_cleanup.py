from types import SimpleNamespace
import threading

import pytest

from parakeet_dictation import transcription
from parakeet_dictation.app import DictationApp
from parakeet_dictation.config import AppConfig


def test_model_cache_is_released_when_inference_fails(monkeypatch, tmp_path):
    calls = []

    def fail(*_args, **_kwargs):
        raise transcription.TranscriptionError("inference failed")

    transcriber = object.__new__(transcription.ParakeetTranscriber)
    transcriber.model = SimpleNamespace(transcribe=fail)
    monkeypatch.setattr(transcription.mx, "clear_cache", lambda: calls.append("clear"))
    monkeypatch.setattr(transcription.gc, "collect", lambda: calls.append("collect"))
    with pytest.raises(transcription.TranscriptionError, match="inference failed"):
        transcriber._transcribe_path(tmp_path / "synthetic.wav")
    assert calls == ["collect", "clear"]


def test_empty_high_accuracy_result_falls_back_without_loading_another_model():
    controller = object.__new__(DictationApp)
    controller.config = AppConfig(high_accuracy=True)
    controller._cancel_event = threading.Event()
    controller.qwen = SimpleNamespace(is_ready=lambda: True)
    controller._qwen_transcribe_recorder_pcm = lambda _pcm: ""
    controller.recorder = SimpleNamespace(channels=1, rate=16000, sample_width=lambda: 2)
    controller.transcriber = SimpleNamespace(transcribe_pcm=lambda *_args, **_kwargs: "Recovered words")
    assert controller._final_transcribe_pcm(b"\x01\x02") == "Recovered words"


def test_cancel_before_inference_does_not_enter_a_model():
    controller = object.__new__(DictationApp)
    controller._cancel_event = threading.Event()
    controller._cancel_event.set()
    with pytest.raises(transcription.TranscriptionError, match="Cancelled"):
        controller._final_transcribe_pcm(b"\x01\x02")


def test_failed_model_download_can_be_retried_without_duplicate_loads(monkeypatch):
    attempts = []
    release = threading.Event()

    def load(_model_id):
        attempts.append(True)
        if len(attempts) == 1:
            raise OSError("offline")
        assert release.wait(timeout=2)
        return SimpleNamespace(transcribe=lambda _path: None)

    monkeypatch.setattr(transcription, "from_pretrained", load)
    monkeypatch.setattr(transcription.ParakeetTranscriber, "_warm_model", lambda self: None)
    transcriber = transcription.ParakeetTranscriber()
    assert transcriber.ready_event.wait(timeout=2)
    with pytest.raises(transcription.TranscriptionError, match="offline"):
        transcriber.wait_until_ready()
    try:
        assert transcriber.retry_loading()
        assert not transcriber.retry_loading()
    finally:
        release.set()
    assert transcriber.ready_event.wait(timeout=2)
    transcriber.wait_until_ready()
    assert transcriber.is_ready()
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
        model.transcribe_pcm(b"\x01\x02" * 100, 1, 2, 16000)
    assert model._active_inferences == 0
    assert calls == ["clear"]


def test_cancel_during_failed_high_accuracy_pass_does_not_start_fallback():
    controller = object.__new__(DictationApp)
    controller.config = AppConfig(high_accuracy=True)
    controller._cancel_event = threading.Event()
    controller.qwen = SimpleNamespace(is_ready=lambda: True)

    def fail(_pcm):
        controller._cancel_event.set()
        raise RuntimeError("failed after cancellation")

    controller._qwen_transcribe_recorder_pcm = fail
    with pytest.raises(transcription.TranscriptionError, match="Cancelled"):
        controller._final_transcribe_pcm(b"\x01\x02")


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
    transcriber = object.__new__(transcription.ParakeetTranscriber)
    transcriber.ready_event = threading.Event()
    transcriber.ready_event.set()
    transcriber.load_error = None
    transcriber.model = SimpleNamespace(
        preprocessor_config=SimpleNamespace(sample_rate=16000, hop_length=160),
        generate=lambda mel: calls.append(("memory", mel)) or [SimpleNamespace(text=" spoken words ")],
        transcribe=lambda path, **kwargs: calls.append(("file", kwargs)) or SimpleNamespace(text="long words"),
    )
    return transcriber


def test_short_dictation_is_recognized_from_memory_without_a_temporary_file(monkeypatch, tmp_path):
    calls = []
    monkeypatch.setattr(transcription, "get_logmel", lambda audio, config: len(audio))
    monkeypatch.setattr(transcription.tempfile, "tempdir", str(tmp_path))
    progress = []
    text = recognizer(calls).transcribe_pcm(b"\x01\x00" * 16000, 1, 2, 16000,
                                            progress_callback=lambda *a: progress.append(a))
    assert text == "spoken words"
    assert calls == [("memory", 16000)]
    assert progress == [(16000, 16000)]
    assert not list(tmp_path.iterdir())


def test_long_dictation_keeps_the_library_chunk_merging(monkeypatch, tmp_path):
    calls = []
    monkeypatch.setattr(transcription.tempfile, "tempdir", str(tmp_path))
    pcm = b"\x01\x00" * (16000 * 121)
    assert recognizer(calls).transcribe_pcm(pcm, 1, 2, 16000) == "long words"
    assert calls == [("file", {"chunk_duration": 120.0, "overlap_duration": 15.0})]
    assert not list(tmp_path.iterdir())  # The temporary WAV is removed.


def test_audio_too_short_for_a_spectrogram_is_no_speech():
    calls = []
    assert recognizer(calls).transcribe_pcm(b"\x01\x00" * 100, 1, 2, 16000) == ""
    assert calls == []


def test_high_accuracy_pass_receives_the_users_vocabulary():
    seen = {}
    model = transcription.QwenTranscriber()
    model.model = SimpleNamespace(transcribe=lambda samples, **kwargs: seen.update(kwargs) or SimpleNamespace(text="ok"))
    assert model.transcribe_pcm(b"\x01\x02" * 100, 1, 2, 16000, context="Vocabulary: Maramax.") == "ok"
    assert seen == {"language": "en", "context": "Vocabulary: Maramax."}
