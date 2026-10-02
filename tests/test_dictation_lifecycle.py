"""The controller's phase transitions, with every collaborator stubbed: no app, audio, or model."""
import threading
from types import SimpleNamespace

import pytest

from parakeet_dictation import app as module
from parakeet_dictation.app import Phase
from parakeet_dictation.capture import CaptureMeter
from parakeet_dictation.config import AppConfig
from parakeet_dictation.recordings import RecordingStore


def controller(monkeypatch):
    app = object.__new__(module.DictationApp)
    calls = []
    app.config = AppConfig()
    app.transcriber = SimpleNamespace(is_ready=lambda: True, status_message=lambda: "Speech model ready",
                                      start_drafts=lambda **_kw: calls.append("drafts") or True,
                                      finish_drafts=lambda: True)
    app.recorder = SimpleNamespace(start=lambda cancel: True, last_error=None, frames=[],
                                  capture_snapshot=CaptureMeter().snapshot, prepare=lambda: calls.append("helper"))
    app._shutting_down = False
    app._session = 0
    app._phase = Phase.IDLE
    app.overlay_visible = False
    app._compact_session = False
    app._stop_when_connected = False
    app._hide_window_when_done = False
    app._drafts_session = None
    app._start_thread = None
    app._start_cancel = threading.Event()
    app._cancel_event = threading.Event()
    app._queue_cancel_event = threading.Event()
    app._recordings_window = None
    app._previous_app = None
    app._capture_health = None
    app._capture_device = ""
    app._capture_warning = ""
    app._resting_status = "Ready"
    app._last_status = ""
    app._paste_target = SimpleNamespace(current=lambda: "the app in front")
    app._show_status = lambda message, revert_after=0: calls.append(message)
    app._push_status = lambda message, revert_after=0: calls.append(message)
    app.overlay_controller = SimpleNamespace(
        prepare_for_recording=lambda: None,
        show_mode=lambda _mode: calls.append("activated window"),
        show_active_microphone=lambda _name: None,
        set_capture=lambda _snapshot: None,
        set_transcribing=lambda _on: None,
        set_queue_processing=lambda _on: None,
        hide=lambda: calls.append("window hidden"),
    )
    app.indicator = SimpleNamespace(show=lambda: calls.append("passive bar"),
                                    set_capture=lambda _snapshot: None,
                                    set_transcribing=lambda: calls.append("bar transcribing"),
                                    finish=lambda message, duration: calls.append(("bar finished", duration)),
                                    hide=lambda: None)
    app.hotkey_manager = SimpleNamespace(set_recording_shortcut=lambda handler: None)
    app.record_menu = SimpleNamespace(title="")
    monkeypatch.setattr(module.AppHelper, "callAfter", lambda *_args: None)
    monkeypatch.setattr(module, "call_later", lambda *_args: None)
    return app, calls


def test_phase_is_one_value_so_recording_and_transcribing_cannot_both_be_true(monkeypatch):
    app, _ = controller(monkeypatch)
    for phase, recording, transcribing in ((Phase.IDLE, False, False), (Phase.CONNECTING, True, False),
                                           (Phase.RECORDING, True, False), (Phase.TRANSCRIBING, False, True)):
        app._phase = phase
        assert (app.recording_active, app.is_transcribing) == (recording, transcribing)
        assert app.is_busy == (phase is not Phase.IDLE)


def test_compact_start_returns_while_the_audio_backend_is_still_opening(monkeypatch):
    app, calls = controller(monkeypatch)
    release = threading.Event()
    entered = threading.Event()

    def open_backend(_cancel):
        entered.set()
        return release.wait(timeout=2)

    app.recorder.start = open_backend
    try:
        assert app.start_recording()
        assert entered.wait(timeout=1)
        assert not release.is_set()
        assert app._phase is Phase.CONNECTING
        assert app._previous_app == "the app in front"
        assert "passive bar" in calls
        assert "activated window" not in calls
    finally:
        release.set()
        if app._start_thread is not None:
            app._start_thread.join(timeout=2)


def test_stop_during_startup_cancels_only_that_attempt_and_is_applied_when_it_returns(monkeypatch):
    app, _ = controller(monkeypatch)
    app._phase = Phase.CONNECTING
    attempt = app._start_cancel
    app.stop_recording_requested(hide_after=True)
    assert app._stop_when_connected and app._hide_window_when_done
    assert attempt.is_set()
    assert app._phase is Phase.CONNECTING  # Nothing is transcribed until the backend answers.
    workers = []
    monkeypatch.setattr(module.threading, "Thread", lambda **kwargs: workers.append(kwargs) or SimpleNamespace(start=lambda: None))
    app._recording_started(True, 0)  # The open won the race: the real stop runs now.
    assert app._phase is Phase.TRANSCRIBING
    assert workers[0]["target"] == app._transcribe_recording_worker


def test_each_recording_gets_a_fresh_cancel_event(monkeypatch):
    app, _ = controller(monkeypatch)
    stale = app._start_cancel
    stale.set()  # A cancel left over from an earlier attempt.
    seen = []
    app.recorder.start = lambda cancel: seen.append(cancel) or True
    assert app.start_recording()
    app._start_thread.join(timeout=2)
    assert seen[0] is app._start_cancel and seen[0] is not stale
    assert not seen[0].is_set()


def test_stale_temporary_hotkey_cannot_stop_a_later_session(monkeypatch):
    app, _ = controller(monkeypatch)
    handlers = []
    stopped = []
    app.hotkey_manager.set_recording_shortcut = handlers.append
    app.stop_recording_requested = lambda: stopped.append(True)
    app._set_recording_shortcut(True)
    app._session += 1
    handlers[0]()
    assert stopped == []
    app._set_recording_shortcut(False)
    assert handlers[-1] is None


def test_late_start_completion_cannot_start_preview_after_capture_failed(monkeypatch):
    app, calls = controller(monkeypatch)
    app._phase = Phase.CONNECTING
    app._monitor_capture = lambda _session: setattr(app, "_phase", Phase.TRANSCRIBING)
    app._recording_started(True, 0)
    assert "drafts" not in calls


def test_live_preview_starts_once_per_recording_in_the_full_window(monkeypatch):
    app, calls = controller(monkeypatch)
    app._phase = Phase.CONNECTING
    app._monitor_capture = lambda _session: None
    app._recording_started(True, 0)
    app._start_drafts_if_wanted()  # Expanding the window again must not start a second stream.
    assert calls.count("drafts") == 1
    app.config.live_preview = False
    app._drafts_session = None
    app._start_drafts_if_wanted()
    assert calls.count("drafts") == 1


def test_expanding_during_transcription_preserves_original_paste_target(monkeypatch):
    app, _ = controller(monkeypatch)
    app._phase = Phase.TRANSCRIBING
    app.current_transcript = ""
    app._previous_app = original = object()
    app._hide_window_when_done = True
    app.open_transcript_window()
    assert app._previous_app is original
    assert app._session == 0
    assert not app._hide_window_when_done  # The user asked to see it: it stays open.


def test_opening_the_window_while_idle_targets_the_app_now_in_front(monkeypatch):
    app, _ = controller(monkeypatch)
    app._previous_app = "an app left long ago"
    app.current_transcript = ""
    app.open_transcript_window()
    assert app._previous_app == "the app in front"
    assert app._session == 1


def test_failed_microphone_start_does_not_leave_connecting_as_the_resting_status(monkeypatch):
    app, calls = controller(monkeypatch)
    app._resting_status = "Connecting microphone…"
    app._phase = Phase.CONNECTING
    app._hide_window_when_done = True
    app.recorder.last_error = RuntimeError("Selected microphone disconnected: AirPods")
    started = []
    monkeypatch.setattr(module.threading, "Thread", lambda **kwargs: started.append(kwargs["target"]) or SimpleNamespace(start=lambda: None))
    app._recording_started(False, 0)
    assert app._resting_status == "Ready"
    assert app._phase is Phase.IDLE and not app._hide_window_when_done
    assert "Selected microphone disconnected" in calls[-1]
    assert started == [app._prepare_recorder]  # A fresh helper, launched off the main thread.


def test_failed_start_in_compact_mode_finishes_the_bar_instead_of_leaving_it_up(monkeypatch):
    app, calls = controller(monkeypatch)
    app._compact_session = True
    app._phase = Phase.CONNECTING
    app.recorder.last_error = RuntimeError("Connection timed out — try again")
    monkeypatch.setattr(module.threading, "Thread", lambda **kwargs: SimpleNamespace(start=lambda: None))
    app._recording_started(False, 0)
    assert calls[-1] == ("bar finished", module.BAR_SECONDS_AFTER_PROBLEM)
    app.transcriber.is_ready = lambda: False
    app.config.compact_dictation = True
    assert not app.start_recording()  # Model not ready: the bar explains, then goes away.
    assert ("bar finished", module.BAR_SECONDS_AFTER_PROBLEM) in calls[-2:]


def test_microphone_start_that_raises_still_hands_the_ui_back(monkeypatch):
    app, _ = controller(monkeypatch)
    handed_back = []
    monkeypatch.setattr(module.AppHelper, "callAfter", lambda fn, *args: handed_back.append((fn, args)))

    def explode(_cancel):
        raise OSError("spill handle could not be closed")

    app.recorder.start = explode
    app._start_recording_worker(0, threading.Event())
    assert handed_back == [(app._recording_started, (False, 0))]
    assert "spill handle" in str(app.recorder.last_error)


def test_audio_helper_failure_ends_the_recording_at_once(monkeypatch):
    app, _ = controller(monkeypatch)
    app._compact_session = True
    app._phase = Phase.RECORDING
    app._capture_device = "System default"
    app.recorder.last_error = RuntimeError("Microphone connection ended unexpectedly")
    stopped = []
    app.stop_recording_requested = lambda: stopped.append(True)
    app._monitor_capture(0)
    assert stopped == [True]
    assert "incomplete" in app._capture_warning


def test_microphone_switch_during_recording_is_reported_not_fatal(monkeypatch):
    app, calls = controller(monkeypatch)
    meter = CaptureMeter()
    meter.feed(b"\x10\x00" * 512)
    meter.device_name = "MacBook Pro Microphone"
    app.recorder.capture_snapshot = meter.snapshot
    app._compact_session = True
    app._capture_health = "reconnecting"
    app._capture_device = "Maxim’s AirPods Pro 2"
    app._phase = Phase.RECORDING
    stopped = []
    app.stop_recording_requested = lambda: stopped.append(True)
    app._monitor_capture(0)
    assert stopped == []
    assert app._capture_warning == "Microphone changed — now using MacBook Pro Microphone"
    assert calls[-1] == "Recording…"


def test_cancelling_a_transcription_takes_effect_before_completion_can_run(monkeypatch):
    app, calls = controller(monkeypatch)
    app._phase = Phase.TRANSCRIBING
    app.dismiss_requested()
    assert app._cancel_event.is_set() and app._queue_cancel_event.is_set()
    assert calls[-1] == "Cancelling…"  # Shown directly, not queued behind the completion.
    app._complete_operation_on_main(0)
    assert app._phase is Phase.IDLE and app._resting_status == "Ready"
    assert calls[-1] == "window hidden"


def test_dismissing_an_idle_window_just_closes_it(monkeypatch):
    app, calls = controller(monkeypatch)
    app.overlay_visible = True
    app.dismiss_requested()
    assert not app.overlay_visible and calls == ["window hidden"]


def test_bar_stays_longer_for_a_problem_than_for_a_clean_result(monkeypatch):
    app, calls = controller(monkeypatch)
    app._compact_session = True
    app._phase = Phase.TRANSCRIBING
    app._complete_operation_on_main(0, module.BAR_SECONDS_AFTER_SUCCESS)
    assert calls[-1] == ("bar finished", module.BAR_SECONDS_AFTER_SUCCESS)
    app._phase = Phase.TRANSCRIBING
    app._complete_operation_on_main(0)
    assert calls[-1] == ("bar finished", module.BAR_SECONDS_AFTER_PROBLEM)


def test_dictation_started_behind_the_queue_dialog_is_not_overrun(monkeypatch):
    app, calls = controller(monkeypatch)
    app.queue = SimpleNamespace(pending_count=lambda: 1, requeue_cancelled=lambda: None)

    def dialog():
        app._phase = Phase.RECORDING  # The hotkey fired inside the modal loop.
        return SimpleNamespace(mode=None)

    app.overlay_controller.show_output_mode_dialog = dialog
    app.queue_start_requested()
    assert app._phase is Phase.RECORDING
    assert calls[-1] == "Finish the current operation first"


def test_dictation_started_behind_the_clear_dialog_keeps_its_audio(monkeypatch, tmp_path):
    app, calls = controller(monkeypatch)
    app.recordings = RecordingStore(tmp_path)
    in_flight = app.recordings.save(b"\x01\x00" * 16000)
    app._support_dir = tmp_path
    app.recorder.discard_recovery = lambda: pytest.fail("the live spill must not be deleted")

    def confirm(**_kwargs):
        app._phase = Phase.TRANSCRIBING  # A dictation began and is being transcribed.
        return 1

    monkeypatch.setattr(module.rumps, "alert", confirm)
    app.clear_history_requested()
    assert [record.id for record in app.recordings.list_recordings()] == [in_flight.id]
    assert "Nothing was cleared" in calls[-1]


def test_recover_prefers_audio_that_was_never_transcribed(monkeypatch, tmp_path):
    app, _ = controller(monkeypatch)
    store = RecordingStore(tmp_path)
    interrupted = store.save(b"\x01\x00" * 16000)
    failed = store.save(b"\x02\x00" * 16000)
    store.update(failed.id, status="failed")
    app.recordings = store
    app._support_dir = tmp_path
    chosen = []
    app.transcribe_recording = chosen.append
    app.recover_last_recording()
    assert chosen == [interrupted.id]
    store.update(interrupted.id, status="done")
    store.update(failed.id, status="done")
    app.recover_last_recording()  # Nothing unfinished: the newest recording is transcribed again.
    assert chosen[-1] == store.list_recordings()[0].id


def test_crash_leftover_is_moved_into_recordings_once(monkeypatch, tmp_path):
    from parakeet_dictation import recovery

    app, _ = controller(monkeypatch)
    pcm = b"\x01\x00" * 16000
    recovery.last_recording_path(tmp_path).write_bytes(pcm + b"\x07")  # An interrupted write: odd length.
    app.recordings = RecordingStore(tmp_path / "recordings")
    app._support_dir = tmp_path
    app._adopt_recovered_audio()
    app._adopt_recovered_audio()
    records = app.recordings.list_recordings()
    assert len(records) == 1
    assert records[0].status == "saved"
    assert "interrupted" in records[0].message
    assert app.recordings.load_pcm(records[0].id) == pcm
    assert not recovery.has_last_recording(tmp_path)


def test_microphone_settings_reach_the_recorder_through_one_place(monkeypatch):
    app, _ = controller(monkeypatch)
    released = []
    app.recorder.release_device = lambda: released.append(True)
    app._settings_path = None
    app._save_settings = lambda: True
    app._preferences_window = None
    threads = []
    monkeypatch.setattr(module.threading, "Thread", lambda **kwargs: threads.append(kwargs["target"]) or SimpleNamespace(start=kwargs["target"]))
    app.select_input_device("AirPods")
    app.set_keep_microphone_ready(120)
    assert (app.recorder.device_name, app.recorder.prefer_builtin, app.recorder.keep_warm_seconds) == ("AirPods", True, 120)
    assert released == [True, True]  # A device kept open under the old settings is let go.
    app._phase = Phase.RECORDING
    app.select_input_device(None)
    assert app.config.input_device == "AirPods"  # Refused mid-recording.


def test_queue_summary_covers_every_outcome():
    summary = module.queue_run_summary
    assert summary(cancelled=False, exported="Copied 2 transcripts to clipboard", export_error=None, any_failed=False) == "Copied 2 transcripts to clipboard"
    assert summary(cancelled=True, exported="Saved 1 file to /x", export_error=None, any_failed=False) == "Queue cancelled. Saved 1 file to /x"
    assert summary(cancelled=True, exported=None, export_error="disk full", any_failed=False) == "Queue cancelled"
    assert summary(cancelled=False, exported=None, export_error="disk full", any_failed=False) == "Export failed: disk full"
    assert summary(cancelled=False, exported=None, export_error=None, any_failed=True) == "All items failed"
    assert summary(cancelled=False, exported=None, export_error=None, any_failed=False) == "No transcription output"


def test_empty_capture_outcomes():
    outcome = module.empty_capture_outcome
    base = dict(has_audio=True, has_signal=True, faint=False, cancelled=False, audio_kept=True)
    assert outcome(**base) == ("failed", "No transcript returned — audio kept for retry")
    assert outcome(**base | {"faint": True})[1] == "No speech heard — signal too faint; audio kept for retry"
    assert outcome(**base | {"has_signal": False})[1] == "Microphone delivered silence — check your input"
    assert outcome(**base | {"has_audio": False, "has_signal": False})[1].startswith("No audio received")
    assert outcome(**base | {"cancelled": True}) == ("cancelled", "Cancelled — audio kept for retry")
    assert outcome(**base | {"cancelled": True, "audio_kept": False})[1] == "Cancelled — could not save audio"
    assert outcome(**base | {"audio_kept": False})[1] == "No transcript returned — could not save audio"


def test_recovery_marks_a_recording_it_tried_so_the_next_press_moves_on(monkeypatch, tmp_path):
    app, calls = controller(monkeypatch)
    store = RecordingStore(tmp_path)
    older = store.save(b"\x01\x00" * 16000)
    newer = store.save(b"\x02\x00" * 16000)
    app.recordings = store
    app._support_dir = tmp_path
    app._final_transcribe_pcm = lambda _pcm: ""
    app._recover_worker(0, newer.id)
    assert {record.id: record.status for record in store.list_recordings()} == {older.id: "saved", newer.id: "failed"}
    chosen = []
    app.transcribe_recording = chosen.append
    app.recover_last_recording()
    assert chosen == [older.id]
