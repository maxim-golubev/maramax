"""Controller regressions without an app, real microphone, or model instance."""

import threading
from types import SimpleNamespace

import pytest

from parakeet_dictation import app as module
from parakeet_dictation.capture import CaptureMeter
from parakeet_dictation.config import AppConfig
from parakeet_dictation.recordings import RecordingStore


def pipeline(tmp_path, monkeypatch, pcm, result=""):
    controller = object.__new__(module.DictationApp)
    meter = CaptureMeter()
    meter.feed(pcm)
    recovery_calls = []
    statuses = []
    published = []
    pending_ui = []
    controller.recorder = SimpleNamespace(
        stop=lambda: pcm,
        capture_snapshot=meter.snapshot,
        abandoned_sessions=0,
        preserve_recovery=lambda **kwargs: recovery_calls.append("preserve") or True,
        discard_recovery=lambda: recovery_calls.append("discard"),
    )
    controller.recordings = RecordingStore(tmp_path)
    controller._capture_at_stop = meter.snapshot()
    controller._capture_warning = ""
    controller._cancel_event = threading.Event()
    controller._finish_live_preview = lambda: True
    controller._final_transcribe_pcm = lambda _pcm: result
    controller._set_current_text_on_main = lambda *_args: None
    controller._push_status = lambda message, *_args, **_kwargs: statuses.append(message)
    controller._publish_transcript = lambda **kwargs: published.append(kwargs["text"])
    controller._complete_transcription_on_main = lambda session: None
    controller.config = AppConfig()
    controller.is_transcribing = True
    monkeypatch.setattr(module.AppHelper, "callAfter", lambda fn, *args: pending_ui.append((fn, args)))
    return controller, recovery_calls, statuses, published, pending_ui


def test_empty_model_result_keeps_recording_instead_of_deleting_it(tmp_path, monkeypatch):
    pcm = b"\x01\x02" * 16000
    controller, recovery, statuses, _, ui = pipeline(tmp_path, monkeypatch, pcm)
    controller._transcribe_recording_worker(None, 1)
    entry = controller.recordings.list_recordings()[0]
    assert entry.status == "failed"
    assert controller.recordings.load_pcm(entry.id) == pcm
    assert "discard" not in recovery
    assert "No transcript returned" in statuses[-1]
    assert ui  # UI completion is always scheduled, even for empty results.
    assert controller.is_transcribing  # Released only on the UI thread.


def test_digital_silence_is_archived_without_running_inference(tmp_path, monkeypatch):
    controller, recovery, statuses, _, _ = pipeline(tmp_path, monkeypatch, bytes(32000))

    def no_inference(_pcm):
        pytest.fail("Digital silence must not be sent to the recognizer")

    controller._final_transcribe_pcm = no_inference
    controller._transcribe_recording_worker(None, 1)
    assert "delivered silence" in statuses[-1]
    assert controller.recordings.list_recordings()[0].status == "failed"
    assert "discard" not in recovery


def test_empty_capture_is_reported_as_capture_failure(tmp_path, monkeypatch):
    controller, recovery, statuses, _, _ = pipeline(tmp_path, monkeypatch, b"")
    controller._transcribe_recording_worker(None, 1)
    assert "No audio received" in statuses[-1]
    assert controller.recordings.list_recordings() == []
    assert "discard" not in recovery


def test_completed_result_survives_late_cancel(tmp_path, monkeypatch):
    controller, recovery, _, published, _ = pipeline(tmp_path, monkeypatch, b"\x01\x02" * 16000)

    def finish(_pcm):
        controller._cancel_event.set()
        return "Completed words"

    controller._final_transcribe_pcm = finish
    controller._transcribe_recording_worker(None, 1)
    assert published == ["Completed words"]
    assert controller.recordings.list_recordings()[0].status == "done"
    assert recovery == ["discard"]


def test_archive_write_failure_keeps_spill_and_publishes_transcript(tmp_path, monkeypatch):
    controller, recovery, _, published, _ = pipeline(tmp_path, monkeypatch, b"\x01\x02" * 16000, "Words")

    def fail(*_args):
        raise OSError("Disk full")

    monkeypatch.setattr(controller.recordings, "save", fail)
    controller._transcribe_recording_worker(None, 1)
    assert published == ["Words"]
    assert recovery == ["preserve"]


def test_inference_error_preserves_audio_and_finishes_ui(tmp_path, monkeypatch):
    controller, recovery, statuses, _, ui = pipeline(tmp_path, monkeypatch, b"\x01\x02" * 16000)

    def fail(_pcm):
        raise module.TranscriptionError("Engine failed")

    controller._final_transcribe_pcm = fail
    controller._transcribe_recording_worker(None, 1)
    assert "Engine failed" in statuses[-1]
    assert recovery == ["preserve"]
    assert controller.recordings.list_recordings()[0].status == "failed"
    assert ui


def test_actual_pcm_wins_over_a_snapshot_taken_before_the_first_frame(tmp_path, monkeypatch):
    controller, _, _, published, _ = pipeline(tmp_path, monkeypatch, b"\x01\x02" * 16000, "Words")
    controller._capture_at_stop = CaptureMeter().snapshot()
    controller._transcribe_recording_worker(None, 1)
    assert published == ["Words"]


def test_total_storage_failure_does_not_claim_the_audio_was_saved(tmp_path, monkeypatch):
    controller, _, statuses, _, _ = pipeline(tmp_path, monkeypatch, b"\x01\x02" * 16000)

    def fail(*_args):
        raise OSError("Disk full")

    monkeypatch.setattr(controller.recordings, "save", fail)
    controller.recorder.preserve_recovery = lambda **_kwargs: False
    controller._transcribe_recording_worker(None, 1)
    assert "could not save audio" in statuses[-1]
