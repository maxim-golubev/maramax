"""The dictation worker with a real archive and real spill files; no app, microphone, or model."""

import threading
from types import SimpleNamespace

import pytest

from parakeet_dictation import app as module
from parakeet_dictation import recovery
from parakeet_dictation.capture import CaptureMeter
from parakeet_dictation.config import AppConfig
from parakeet_dictation.isolated_recorder import IsolatedAudioRecorder
from parakeet_dictation.recordings import RecordingStore


def pipeline(tmp_path, monkeypatch, pcm, result=""):
    controller = object.__new__(module.DictationApp)
    meter = CaptureMeter()
    meter.feed(pcm)
    statuses = []
    published = []
    pending_ui = []
    drafts_stopped = []
    # A real recorder that never starts a helper: it owns the spill files,
    # so these tests see what is actually left on disk.
    recorder = IsolatedAudioRecorder(tmp_path, command=["/usr/bin/false"])
    recorder.stop = lambda: pcm
    recorder.capture_snapshot = meter.snapshot
    recorder.prepare = lambda: None
    if pcm:
        recovery.in_progress_path(tmp_path).write_bytes(pcm)
    controller.recorder = recorder
    controller.recordings = RecordingStore(tmp_path / "recordings")
    controller._support_dir = tmp_path
    controller._capture_at_stop = meter.snapshot()
    controller._capture_warning = ""
    controller._cancel_event = threading.Event()
    controller._shutting_down = False
    controller._final_transcribe_pcm = lambda _pcm: result
    controller.transcriber = SimpleNamespace(finish_drafts=lambda: drafts_stopped.append(True) or True)
    controller.drafts_stopped = drafts_stopped
    controller._set_current_text_on_main = lambda *_args: None
    controller._push_status = lambda message, revert_after=0: statuses.append(message)
    controller._publish_transcript = lambda text, *_args: (published.append(text) or text, True)
    controller.config = AppConfig()
    controller._phase = module.Phase.TRANSCRIBING
    monkeypatch.setattr(module.AppHelper, "callAfter", lambda fn, *args: pending_ui.append((fn, args)))
    return controller, statuses, published, pending_ui


def spill_files(tmp_path):
    return sorted(path.name for path in tmp_path.glob("*.pcm"))


def test_empty_model_result_keeps_the_recording_in_the_archive_only(tmp_path, monkeypatch):
    pcm = b"\x01\x02" * 16000
    controller, statuses, _, ui = pipeline(tmp_path, monkeypatch, pcm)
    controller._transcribe_recording_worker(1)
    entry = controller.recordings.list_recordings()[0]
    assert entry.status == "failed"
    assert controller.recordings.load_pcm(entry.id) == pcm
    assert "No transcript returned — audio kept for retry" == statuses[-1]
    # One copy, in the archive: a second copy in the spill would come back
    # as a phantom "unsaved recording" at the next launch.
    assert spill_files(tmp_path) == []
    assert ui[-1][0] == controller._complete_operation_on_main
    assert ui[-1][1] == (1, module.BAR_SECONDS_AFTER_PROBLEM)
    assert controller.is_transcribing  # Released only on the UI thread.


def test_digital_silence_is_archived_without_running_inference(tmp_path, monkeypatch):
    controller, statuses, _, _ = pipeline(tmp_path, monkeypatch, bytes(32000))

    def no_inference(_pcm):
        pytest.fail("Digital silence must not be sent to the recognizer")

    controller._final_transcribe_pcm = no_inference
    controller._transcribe_recording_worker(1)
    assert "delivered silence" in statuses[-1]
    assert controller.recordings.list_recordings()[0].status == "failed"
    assert spill_files(tmp_path) == []
    # No recognition ran, but the draft stream must still be told to stop.
    assert controller.drafts_stopped == [True]


def test_empty_capture_is_reported_as_capture_failure(tmp_path, monkeypatch):
    controller, statuses, _, _ = pipeline(tmp_path, monkeypatch, b"")
    controller._transcribe_recording_worker(1)
    assert "No audio received" in statuses[-1]
    assert controller.recordings.list_recordings() == []


def test_completed_result_survives_late_cancel(tmp_path, monkeypatch):
    controller, _, published, ui = pipeline(tmp_path, monkeypatch, b"\x01\x02" * 16000)

    def finish(_pcm):
        controller._cancel_event.set()
        return "Completed words"

    controller._final_transcribe_pcm = finish
    controller._transcribe_recording_worker(1)
    assert published == ["Completed words"]
    assert controller.recordings.list_recordings()[0].status == "done"
    assert spill_files(tmp_path) == []
    assert ui[-1][1] == (1, module.BAR_SECONDS_AFTER_SUCCESS)


def test_archive_write_failure_keeps_spill_and_publishes_transcript(tmp_path, monkeypatch):
    pcm = b"\x01\x02" * 16000
    controller, _, published, _ = pipeline(tmp_path, monkeypatch, pcm, "Words")

    def fail(*_args):
        raise OSError("Disk full")

    monkeypatch.setattr(controller.recordings, "save", fail)
    controller._transcribe_recording_worker(1)
    assert published == ["Words"]
    # The spill is the only copy, so it is kept.
    assert [recovery.load_unsaved(path) for path in recovery.unsaved_recordings(tmp_path)] == [pcm]


def test_every_dictation_keeps_its_audio_while_archiving_keeps_failing(tmp_path, monkeypatch):
    controller, statuses, _, _ = pipeline(tmp_path, monkeypatch, b"")

    def fail(*_args):
        raise OSError("Disk full")

    monkeypatch.setattr(controller.recordings, "save", fail)
    captures = [b"\x01\x00" * 16000 * 60, b"\x02\x00" * 16000 * 10, b"\x03\x00" * 16000 * 90]
    for pcm in captures:
        recovery.in_progress_path(tmp_path).write_bytes(pcm)
        controller.recorder.stop = lambda pcm=pcm: pcm
        controller._transcribe_recording_worker(1)
        assert statuses[-1] == "No transcript returned — audio kept for retry"
    # A shorter capture used to be deleted, and a longer one replaced the first.
    assert [recovery.load_unsaved(path) for path in recovery.unsaved_recordings(tmp_path)] == captures


def test_a_recording_saved_without_its_details_is_not_also_kept_for_recovery(tmp_path, monkeypatch):
    from parakeet_dictation import recordings

    def fail(*_args):
        raise OSError("No space left on device")

    controller, _, published, _ = pipeline(tmp_path, monkeypatch, b"\x01\x02" * 16000, "Words")
    monkeypatch.setattr(recordings, "write_text_atomically", fail)
    controller._transcribe_recording_worker(1)
    assert published == ["Words"]
    assert len(controller.recordings.list_recordings()) == 1
    assert spill_files(tmp_path) == []  # Otherwise the next launch archives it a second time.


def test_inference_error_keeps_the_audio_once_and_finishes_ui(tmp_path, monkeypatch):
    pcm = b"\x01\x02" * 16000
    controller, statuses, _, ui = pipeline(tmp_path, monkeypatch, pcm)

    def fail(_pcm):
        raise module.TranscriptionError("Engine failed")

    controller._final_transcribe_pcm = fail
    controller._transcribe_recording_worker(1)
    assert statuses[-1] == "Engine failed — recording saved (see Recordings)"
    entry = controller.recordings.list_recordings()[0]
    assert entry.status == "failed" and controller.recordings.load_pcm(entry.id) == pcm
    assert spill_files(tmp_path) == []
    assert ui[-1][0] == controller._complete_operation_on_main


def test_unexpected_error_still_settles_audio_and_releases_the_ui(tmp_path, monkeypatch):
    controller, statuses, _, ui = pipeline(tmp_path, monkeypatch, b"\x01\x02" * 16000)

    def crash(_pcm):
        raise ZeroDivisionError("bug")

    controller._final_transcribe_pcm = crash
    controller._transcribe_recording_worker(1)
    assert statuses[-1] == "Transcription failed — recording saved (see Recordings)"
    assert len(controller.recordings.list_recordings()) == 1
    assert ui[-1][0] == controller._complete_operation_on_main


def test_cancelled_dictation_is_archived_as_cancelled(tmp_path, monkeypatch):
    controller, statuses, _, _ = pipeline(tmp_path, monkeypatch, b"\x01\x02" * 16000)

    def cancelled(_pcm):
        controller._cancel_event.set()
        raise module.TranscriptionCancelled("Cancelled")

    controller._final_transcribe_pcm = cancelled
    controller._transcribe_recording_worker(1)
    assert statuses[-1] == "Cancelled — audio kept for retry"
    assert controller.recordings.list_recordings()[0].status == "cancelled"
    assert spill_files(tmp_path) == []


def test_a_real_failure_after_esc_is_reported_as_a_failure(tmp_path, monkeypatch):
    controller, statuses, _, _ = pipeline(tmp_path, monkeypatch, b"\x01\x02" * 16000)
    logged = []
    monkeypatch.setattr(module.logger, "error", logged.append)

    def failed(_pcm):
        controller._cancel_event.set()  # Esc, while the model was failing to load.
        raise module.TranscriptionError("Model failed to load")

    controller._final_transcribe_pcm = failed
    controller._transcribe_recording_worker(1)
    assert statuses[-1] == "Model failed to load — recording saved (see Recordings)"
    assert controller.recordings.list_recordings()[0].status == "failed"
    assert logged == ["Model failed to load"]


def test_actual_pcm_wins_over_a_snapshot_taken_before_the_first_frame(tmp_path, monkeypatch):
    controller, _, published, _ = pipeline(tmp_path, monkeypatch, b"\x01\x02" * 16000, "Words")
    controller._capture_at_stop = CaptureMeter().snapshot()
    controller._transcribe_recording_worker(1)
    assert published == ["Words"]


def test_total_storage_failure_does_not_claim_the_audio_was_saved(tmp_path, monkeypatch):
    controller, statuses, _, _ = pipeline(tmp_path, monkeypatch, b"\x01\x02" * 16000)

    def fail(*_args):
        raise OSError("Disk full")

    monkeypatch.setattr(controller.recordings, "save", fail)
    controller.recorder.preserve_recovery = lambda: False
    controller._transcribe_recording_worker(1)
    assert "could not save audio" in statuses[-1]


def test_barely_audible_capture_is_named_as_such(tmp_path, monkeypatch):
    controller, statuses, _, _ = pipeline(tmp_path, monkeypatch, b"\x0f\x00\xf4\xff" * 8000)
    controller._transcribe_recording_worker(1)
    assert statuses[-1] == "No speech heard — signal too faint; audio kept for retry"


def test_interrupted_microphone_is_reported_with_the_result(tmp_path, monkeypatch):
    controller, statuses, published, ui = pipeline(tmp_path, monkeypatch, b"\x01\x02" * 16000, "Words")
    controller.recorder.last_error = RuntimeError("Microphone connection ended unexpectedly")
    controller._transcribe_recording_worker(1)
    assert published == ["Words"]
    assert "interrupted" in statuses[-1]
    assert ui[-1][1] == (1, module.BAR_SECONDS_AFTER_PROBLEM)  # Long enough to read the warning.


def test_crash_during_recognition_leaves_one_copy_not_two(tmp_path, monkeypatch):
    pcm = b"\x01\x02" * 16000
    controller, _, _, _ = pipeline(tmp_path, monkeypatch, pcm)
    seen = []

    def recognizing(_pcm):
        # What is on disk at the moment the process could die.
        seen.append((spill_files(tmp_path), [r.status for r in controller.recordings.list_recordings()]))
        return "Words"

    controller._final_transcribe_pcm = recognizing
    controller._transcribe_recording_worker(1)
    assert seen == [([], ["saved"])]  # Archived and waiting; nothing for the next launch to duplicate.


def test_recovering_digital_silence_marks_it_tried_so_the_next_press_moves_on(tmp_path, monkeypatch):
    controller, statuses, _, ui = pipeline(tmp_path, monkeypatch, b"")
    silent = controller.recordings.save(bytes(32000))
    older = controller.recordings.save(b"\x01\x00" * 16000)
    controller.recordings.update(older.id, status=module.RecordingStatus.SAVED)
    controller._recover_worker(1, silent.id)
    assert "digital silence" in statuses[-1]
    statuses_now = {record.id: record.status for record in controller.recordings.list_recordings()}
    assert statuses_now[silent.id] == module.RecordingStatus.FAILED
    assert module.recovery_candidate(controller.recordings.list_recordings()).id == older.id
