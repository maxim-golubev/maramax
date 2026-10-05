"""The controller's phase transitions, with every collaborator stubbed: no app, audio, or model."""
import threading
from types import SimpleNamespace

import pytest

from parakeet_dictation import app as module
from parakeet_dictation.app import Phase
from parakeet_dictation.capture import CaptureMeter
from parakeet_dictation.config import AppConfig
from parakeet_dictation.file_queue import TranscriptionQueue
from parakeet_dictation.hotkeys import KEY_NAMES, controlKey, optionKey
from parakeet_dictation.recordings import RecordingStore


def controller(monkeypatch):
    app = object.__new__(module.DictationApp)
    calls = []
    app.config = AppConfig()
    app.transcriber = SimpleNamespace(is_ready=lambda: True, status_message=lambda: "Speech model ready", load_error=None,
                                      start_drafts=lambda **_kw: calls.append("drafts") or True,
                                      finish_drafts=lambda: True)
    app.qwen = SimpleNamespace(failed=False, unload=lambda: calls.append("qwen unloaded"))
    app.retry_model_item = SimpleNamespace(hidden=True)
    app.recorder = SimpleNamespace(start=lambda cancel: True, last_error=None, frames=[],
                                  capture_snapshot=CaptureMeter().snapshot, prepare=lambda: calls.append("helper"))
    app._shutting_down = False
    app._hotkey_error_message = None
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
    app._preferences_window = None
    app._welcome_window = None
    app.history_store = SimpleNamespace(render=lambda: None)
    app._previous_app = None
    app.current_transcript = ""
    app._capture_health = None
    app._capture_device = ""
    app._capture_warning = ""
    app._resting_status = "Ready"
    app._last_status = ""
    app._last_revert = 0
    app._before_cancelling = (app._last_status, 0)
    app._adopting = False
    app._paste_target = SimpleNamespace(current=lambda: "the app in front")
    app._show_status = lambda message, revert_after=0: calls.append(message)
    app._push_status = lambda message, revert_after=0: calls.append(message)
    app.overlay_controller = SimpleNamespace(
        prepare_for_recording=lambda: None,
        show_mode=lambda _mode: calls.append("activated window"),
        focus=lambda: calls.append("focused"),
        set_history_text=lambda _text: None,
        set_intro_text=lambda _text: None,
        show_active_microphone=lambda _name: None,
        set_capture=lambda _snapshot: None,
        set_transcribing=lambda _on: None,
        set_queue_processing=lambda _on: None,
        set_queue_files=lambda _files: None,
        hide=lambda: calls.append("window hidden"),
    )
    app._dictate = module.dictation_shortcut(0x31, 1 << 11, KEY_NAMES)
    app.indicator = SimpleNamespace(show=lambda shortcut, placement: calls.extend(
                                        ["passive bar", ("bar names", shortcut), ("bar at", placement)]),
                                    place=lambda placement: calls.append(("bar moved to", placement)),
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
    app._dictate = module.dictation_shortcut(0x02, controlKey | optionKey, KEY_NAMES)  # Not the default.
    try:
        assert app.start_recording()
        assert entered.wait(timeout=1)
        assert not release.is_set()
        assert app._phase is Phase.CONNECTING
        assert app._previous_app == "the app in front"
        assert "passive bar" in calls and ("bar names", "Control+Option+D") in calls
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


def test_opening_the_window_while_idle_takes_over_the_display(monkeypatch):
    """The paste target is chosen when a dictation starts, not here."""
    app, _ = controller(monkeypatch)
    app.current_transcript = ""
    app.open_transcript_window()
    assert app._session == 1


def test_failed_microphone_start_does_not_leave_connecting_as_the_resting_status(monkeypatch):
    app, calls = controller(monkeypatch)
    app._resting_status = module.WAIT_TO_SPEAK_STATUS
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
    app._last_status = module.COPIED_STATUS
    app._complete_operation_on_main(0)
    assert calls[-1] == ("bar finished", module.BAR_SECONDS_AFTER_SUCCESS)
    # A paste that could not happen, a transcript kept in Maramax, a warning: each gets time to be read.
    for shown in (module.NOT_PERMITTED_STATUS, module.NOT_COPIED_STATUS, module.INCOMPLETE_STATUS):
        app._phase = Phase.TRANSCRIBING
        app._last_status = shown
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
    (tmp_path / recovery.LEGACY_NAME).write_bytes(pcm + b"\x07")  # 0.6.x's slot, an interrupted write.
    app.recordings = RecordingStore(tmp_path / "recordings")
    app._support_dir = tmp_path
    app._adopt_recovered_audio()
    app._adopt_recovered_audio()
    records = app.recordings.list_recordings()
    assert len(records) == 1
    assert records[0].status == "saved"
    assert "interrupted" in records[0].message
    assert app.recordings.load_pcm(records[0].id) == pcm
    assert recovery.unsaved_recordings(tmp_path) == []


def test_every_unsaved_recording_is_adopted_oldest_first(monkeypatch, tmp_path):
    from parakeet_dictation import recovery

    app, _ = controller(monkeypatch)
    app.recordings = RecordingStore(tmp_path / "recordings")
    app._support_dir = tmp_path
    captures = [bytes([n, 0]) * 16000 for n in (1, 2, 3)]
    for pcm in captures:
        recovery.in_progress_path(tmp_path).write_bytes(pcm)
        recovery.promote_in_progress(tmp_path)
    app._adopt_recovered_audio()
    newest_first = [app.recordings.load_pcm(record.id) for record in app.recordings.list_recordings()]
    assert newest_first == captures[::-1]
    assert recovery.unsaved_recordings(tmp_path) == []


def test_recover_transcribes_an_unsaved_recording_into_the_archive(monkeypatch, tmp_path):
    from parakeet_dictation import recovery

    app, _ = controller(monkeypatch)
    app.recordings = RecordingStore(tmp_path / "recordings")
    app._support_dir = tmp_path
    pcm = b"\x01\x00" * 16000
    recovery.in_progress_path(tmp_path).write_bytes(pcm)
    recovery.promote_in_progress(tmp_path)
    chosen = []
    app.transcribe_recording = chosen.append
    app.recover_last_recording()
    assert chosen == recovery.unsaved_recordings(tmp_path)
    app._final_transcribe_pcm = lambda _pcm: "Words"
    app._publish_transcript = lambda text, *_args: text
    app._recover_worker(0, chosen[0])
    records = app.recordings.list_recordings()
    assert [(record.status, record.text) for record in records] == [("done", "Words")]
    assert app.recordings.load_pcm(records[0].id) == pcm
    assert recovery.unsaved_recordings(tmp_path) == []


def test_a_media_failure_after_esc_is_reported_and_logged(monkeypatch):
    app, calls = controller(monkeypatch)
    logged = []
    monkeypatch.setattr(module.logger, "error", logged.append)

    def failed(*_args):
        app._cancel_event.set()
        raise module.TranscriptionError("Could not process talk.mp4: invalid data")

    app._final_transcribe_file = failed
    app._transcribe_file_worker("/tmp/talk.mp4", "talk.mp4", 0)
    assert calls[-1] == "Could not process talk.mp4: invalid data"
    assert logged == ["Could not process talk.mp4: invalid data"]


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
    app.history_store = SimpleNamespace(history_limit=100, render=lambda: None)
    app.set_history_limit(500)
    assert app.config.history_limit == app.history_store.history_limit == 500
    app.recordings = SimpleNamespace(limit=20)
    app._recordings_window = None
    app.set_recordings_limit(50)
    assert app.config.recordings_limit == app.recordings.limit == 50
    assert (app.recorder.device_name, app.recorder.prefer_builtin, app.recorder.keep_warm_seconds) == ("AirPods", True, 120)
    assert released == [True, True]  # A device kept open under the old settings is let go.
    app._phase = Phase.RECORDING
    app.select_input_device(None)
    assert app.config.input_device == "AirPods"  # Refused mid-recording.


def test_queue_summary_covers_every_outcome_and_says_why_files_failed():
    summary = module.queue_run_summary
    ffmpeg = "Importing media needs FFmpeg — install it with brew install ffmpeg"
    assert summary(cancelled=False, exported="Copied 2 transcripts to clipboard", export_error=None,
                   failures=[]) == "Copied 2 transcripts to clipboard"
    assert summary(cancelled=True, exported="Saved 1 transcript to Transcripts", export_error=None,
                   failures=[]) == "Queue cancelled. Saved 1 transcript to Transcripts"
    assert summary(cancelled=True, exported=None, export_error="disk full", failures=[]) == "Queue cancelled"
    assert summary(cancelled=False, exported=None, export_error="disk full", failures=[]) == "Export failed: disk full"
    assert summary(cancelled=False, exported="Copied 1 transcript to clipboard", export_error=None,
                   failures=[ffmpeg]) == "Copied 1 transcript to clipboard — 1 file failed, see the Queue tab"
    # One reason for every failure is the one worth reading: FFmpeg is missing.
    assert summary(cancelled=False, exported=None, export_error=None,
                   failures=[ffmpeg, ffmpeg, ffmpeg]) == f"Nothing transcribed — 3 files failed ({ffmpeg})"
    assert summary(cancelled=False, exported=None, export_error=None,
                   failures=[ffmpeg, "No speech detected"]) == "Nothing transcribed — 2 files failed (see the Queue tab)"
    assert summary(cancelled=False, exported=None, export_error=None, failures=[]) == "No files were transcribed"


def test_empty_capture_outcomes():
    outcome = module.empty_capture_outcome
    base = dict(has_audio=True, has_signal=True, faint=False, cancelled=False, place=module.AudioPlace.ARCHIVED,
                ready=True)
    assert outcome(**base) == ("failed", "No speech detected — audio saved in Recordings")
    assert outcome(**base | {"faint": True})[1] == "No speech heard — the microphone was too quiet; audio saved in Recordings"
    assert outcome(**base | {"has_signal": False})[1] == "The microphone sent only silence — check your input and Privacy & Security → Microphone"
    assert outcome(**base | {"has_audio": False, "has_signal": False})[1].startswith("No audio from the microphone")
    assert outcome(**base | {"cancelled": True}) == ("cancelled", "Cancelled — audio saved in Recordings")
    assert outcome(**base | {"cancelled": True, "place": module.AudioPlace.LOST})[1] == "Cancelled — the audio could not be saved"
    assert outcome(**base | {"place": module.AudioPlace.LOST})[1] == "No speech detected — the audio could not be saved"
    assert outcome(**base | {"place": module.AudioPlace.UNSAVED})[1] == (
        "No speech detected — audio kept; it moves to Recordings at the next launch")


def test_a_failure_reads_as_outcome_then_explanation_on_the_bar():
    from parakeet_dictation.indicator import split_status
    assert split_status(module.failure_text("Engine failed", module.AudioPlace.ARCHIVED)) == ("Engine failed", "Audio saved in Recordings")
    stalled = module.failure_text(module.ENGINE_STALLED, module.AudioPlace.ARCHIVED)       # Keeps its own explanation first.
    assert split_status(stalled) == ("Transcription engine stalled", "Restart the app; audio saved in Recordings")


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


def test_the_shortcut_dictates_from_an_open_idle_window(monkeypatch):
    app, calls = controller(monkeypatch)
    app.overlay_visible = True
    app.dictation_hotkey_pressed()
    app._start_thread.join(timeout=2)
    assert app._phase is Phase.CONNECTING and not app._compact_session  # In the window, with live preview.
    assert "activated window" in calls and "focused" not in calls
    app._phase = Phase.TRANSCRIBING
    app.dictation_hotkey_pressed()
    assert calls[-1] == "focused"            # Busy: it only brings the window forward.


def test_the_shortcut_dictates_on_the_bar_where_the_user_left_it(monkeypatch):
    app, calls = controller(monkeypatch)
    app.config.bar_position = [0.25, 0.75]
    app.dictation_hotkey_pressed()
    app._start_thread.join(timeout=2)
    assert app._phase is Phase.CONNECTING and app._compact_session
    assert ("bar at", [0.25, 0.75]) in calls


def test_dragging_the_bar_is_remembered_and_settings_can_put_it_back(monkeypatch):
    app, calls = controller(monkeypatch)
    saved = []
    app._save_settings = lambda: saved.append(app.config.bar_position) or True
    app.bar_moved((0.1, 0.9))
    assert app.config.bar_position == [0.1, 0.9] and saved == [[0.1, 0.9]]
    app.bar_moved(None)                      # Dropped beside its default place.
    assert app.config.bar_position is None
    app.config.bar_position = [0.5, 0.5]
    app.reset_bar_position()
    assert app.config.bar_position is None and saved[-1] is None
    assert calls[-1] == ("bar moved to", None)          # An open bar goes back at once.


def test_cancelling_while_the_microphone_connects_closes_the_window(monkeypatch):
    app, calls = controller(monkeypatch)
    app.overlay_visible = True
    app._phase = Phase.CONNECTING
    app.dismiss_requested()                  # Esc or Close while it says "Don’t speak yet".
    monkeypatch.setattr(module.threading, "Thread", lambda **kwargs: SimpleNamespace(start=lambda: None))
    app._recording_started(False, 0)
    assert "window hidden" in calls and not app.overlay_visible
    assert "Connection cancelled" in calls and not app._hide_window_when_done


def test_settings_and_recordings_are_told_when_the_app_becomes_busy_or_idle(monkeypatch):
    app, calls = controller(monkeypatch)
    app._preferences_window = SimpleNamespace(show_busy_state=lambda: calls.append(("settings", app.is_busy)),
                                              shows_microphones=lambda: False)
    app._recordings_window = SimpleNamespace(show_busy_state=lambda: calls.append(("recordings", app.is_busy)),
                                             stop_playback=lambda: None, refresh=lambda: None)
    app.start_recording()
    app._start_thread.join(timeout=2)
    app._recording_started(True, app._session)
    app._complete_operation_on_main(app._session)
    told = [call for call in calls if isinstance(call, tuple) and call[0] in ("settings", "recordings")]
    assert told == [("settings", True), ("recordings", True), ("settings", False), ("recordings", False)]


def test_files_added_during_a_recording_wait_in_the_queue(monkeypatch):
    app, calls = controller(monkeypatch)
    app.queue = TranscriptionQueue()
    app.current_transcript = ""
    app._phase = Phase.RECORDING
    app.queue_add_files(["/x/a.m4a", "/x/b.m4a"])
    assert len(app.queue.items()) == 2 and "activated window" not in calls  # Stop and the draft stay in view.
    app.menu_open_files(None)
    assert calls[-1] == "Finish the current operation first"              # Before the file panel, not after it.
    app._phase = Phase.IDLE
    app.queue_add_files(["/x/c.m4a"])
    assert calls[-1] == "activated window"


def test_an_update_waits_for_an_outcome_on_the_bar_and_for_audio_being_saved(monkeypatch):
    app, _ = controller(monkeypatch)
    showing, saving = [False], [False]
    app.indicator.is_finished = lambda: showing[0]
    assert not app._is_in_use()
    showing[0] = True                       # "No speech detected" is on the bar for its 8 s.
    assert app._is_in_use()
    showing[0] = False
    app._recordings_window = SimpleNamespace(is_saving=lambda: saving[0])
    assert not app._is_in_use()
    saving[0] = True                        # Save Audio is still copying the WAV.
    assert app._is_in_use()
    saving[0] = False
    app._phase = Phase.RECORDING
    assert app._is_in_use()




def test_recovered_audio_is_dated_when_it_was_spoken(monkeypatch, tmp_path):
    """A capture whose archive write failed is moved in at the next launch,
    after later dictations: it must not be listed, or pruned, as the newest."""
    import os
    import time

    from parakeet_dictation import recovery

    app, _ = controller(monkeypatch)
    app.recordings = RecordingStore(tmp_path / "recordings")
    app._support_dir = tmp_path
    recovery.in_progress_path(tmp_path).write_bytes(b"\x01\x00" * 16000)
    spoken = time.time() - 2 * 24 * 3600
    os.utime(recovery.in_progress_path(tmp_path), (spoken, spoken))
    recovery.promote_in_progress(tmp_path)  # Its archive write failed.
    later = app.recordings.save(b"\x02\x00" * 16000)
    app._adopt_recovered_audio()
    newest_first = app.recordings.list_recordings()
    assert [record.id for record in newest_first][0] == later.id
    assert app.recordings.load_pcm(newest_first[1].id) == b"\x01\x00" * 16000


def test_copy_last_transcript_falls_back_to_history_after_a_relaunch(monkeypatch):
    app, calls = controller(monkeypatch)
    app.current_transcript = ""
    app.overlay_controller.current_text = ""
    copied = []
    app._copy_text_with_feedback = lambda text, **_kw: copied.append(text) or True
    app.history_store.list_entries = lambda: []
    app.copy_current_transcript()
    assert calls[-1] == "No transcript to copy" and copied == []
    app.history_store.list_entries = lambda: [SimpleNamespace(text="Newest words "), SimpleNamespace(text="Older")]
    app.copy_current_transcript()
    assert copied == ["Newest words"]


def test_the_idle_status_keeps_saying_the_shortcut_does_not_work(monkeypatch):
    """Finishing a dictation started from the menu must not bury a shortcut problem under "Ready"."""
    app, _ = controller(monkeypatch)
    assert app._idle_status() == "Ready"
    app._hotkey_error_message = module.unregistered_status("Option+Space")
    app._compact_session = False
    app._refresh_recordings_window = lambda: None
    app.indicator.finish = lambda message, duration: None
    app._complete_operation_on_main(0)
    assert app._resting_status == module.unregistered_status("Option+Space")
    app._hotkey_error_message = None
    app.transcriber.is_ready = lambda: False
    assert app._idle_status() == "Speech model ready"   # The stub's status message: not "Ready" before it is.


def test_the_menu_offers_a_model_retry_only_after_a_failed_load(monkeypatch):
    app, _ = controller(monkeypatch)
    app.retry_model_item.hidden = None
    app.transcriber.load_error = None
    app._show_model_state()
    assert app.retry_model_item.hidden is True
    app.transcriber.load_error = RuntimeError("offline")
    app._show_model_state()
    assert app.retry_model_item.hidden is False
    # A failed high-accuracy model stops needing a retry once it is turned off.
    app.transcriber.load_error = None
    app.config.high_accuracy = True
    app.qwen.failed = True
    app._show_model_state()
    assert app.retry_model_item.hidden is False
    app._save_settings = lambda: True
    app.toggle_setting("high_accuracy")
    assert app.retry_model_item.hidden is True


def test_the_menu_shows_the_dictation_shortcut_as_the_layout_names_it(monkeypatch):
    from AppKit import NSEventModifierFlagControl, NSEventModifierFlagOption

    app, _ = controller(monkeypatch)
    shown = {}
    app.record_menu = SimpleNamespace(_menuitem=SimpleNamespace(
        setKeyEquivalent_=lambda key: shown.update(key=key),
        setKeyEquivalentModifierMask_=lambda mask: shown.update(mask=mask)))
    monkeypatch.setattr(module, "layout_key_names", lambda: KEY_NAMES)
    app._show_shortcut_in_menu()
    assert shown == {"key": " ", "mask": NSEventModifierFlagOption}          # Option+Space
    app._dictate = module.dictation_shortcut(0x02, controlKey | optionKey, KEY_NAMES)
    app._show_shortcut_in_menu()
    assert shown == {"key": "d", "mask": NSEventModifierFlagControl | NSEventModifierFlagOption}


def test_pressing_the_shortcut_after_a_failed_model_load_tries_again(monkeypatch):
    app, calls = controller(monkeypatch)
    app.transcriber.is_ready = lambda: False
    app.transcriber.load_error = RuntimeError("offline")
    app.retry_speech_model = lambda: calls.append("retry")
    assert not app.start_recording()
    assert "retry" in calls
    app.transcriber.load_error = None                     # Still loading: nothing to retry.
    calls.clear()
    app.start_recording()
    assert "retry" not in calls


def test_retry_also_reloads_a_high_accuracy_model_whose_download_failed(monkeypatch):
    app, calls = controller(monkeypatch)
    app.config.high_accuracy = True
    app.transcriber.retry_loading = lambda: False                    # The standard model is fine.
    app.qwen = SimpleNamespace(failed=True, start_loading=lambda: calls.append("qwen load"),
                               status_message=lambda: "Loading the high-accuracy model…")
    app.retry_model_item = SimpleNamespace(hidden=True)
    app._preferences_window = None
    assert app.models_failed()
    app.retry_speech_model()
    assert "qwen load" in calls
    app.config.high_accuracy = False                                 # Not wanted: nothing to retry.
    assert not app.models_failed()


def test_stopping_while_the_microphone_still_connects_is_not_blamed_on_the_microphone():
    outcome = module.empty_capture_outcome(has_audio=True, has_signal=False, faint=True, cancelled=False,
                                           place=module.AudioPlace.ARCHIVED, ready=False)
    assert outcome == ("cancelled", "Stopped before the microphone was ready — nothing was recorded")


def test_a_confirmed_clear_deletes_every_recording_and_kept_capture(monkeypatch, tmp_path):
    from parakeet_dictation import recovery

    app, calls = controller(monkeypatch)
    app.recordings = RecordingStore(tmp_path / "recordings")
    app.recordings.save(b"\x01\x00" * 16000)
    app._support_dir = tmp_path
    recovery.keep_unsaved(tmp_path, b"\x02\x00" * 16000)                 # From a failed archive.
    recovery.in_progress_path(tmp_path).write_bytes(b"\x03\x00" * 16000)  # One that could not be set aside.
    app.recorder.discard_recovery = lambda: None
    app.history_store = SimpleNamespace(render=lambda: None, clear=lambda: True)
    app.current_transcript = "words"
    app.overlay_controller.set_current_text = lambda _text: None
    monkeypatch.setattr(module.rumps, "alert", lambda **_kwargs: 1)
    app.clear_history_requested()
    assert app.recordings.list_recordings() == [] and recovery.unsaved_recordings(tmp_path) == []
    assert not recovery.in_progress_path(tmp_path).exists()
    assert calls[-1] == "History and recordings cleared"


def test_a_cancel_that_came_after_the_outcome_leaves_the_outcome_said(monkeypatch):
    app, calls = controller(monkeypatch)
    shown = []
    app._show_status = lambda message, revert_after=0: shown.append((message, revert_after)) or setattr(
        app, "_last_status", message)
    app._phase = Phase.TRANSCRIBING
    app._last_status, app._last_revert = module.COPIED_STATUS, 5    # Said, then Esc before completion.
    app.dismiss_requested()
    app._complete_operation_on_main(0)
    assert shown == [(module.CANCELLING_STATUS, 0), (module.COPIED_STATUS, 5)]


def test_recovered_audio_being_moved_in_is_neither_recovered_nor_cleared_meanwhile(monkeypatch):
    app, calls = controller(monkeypatch)
    app._adopting = True
    app.recordings = SimpleNamespace(list_recordings=lambda: calls.append("listed") or [])
    alerts = []
    monkeypatch.setattr(module.rumps, "alert", lambda **kwargs: alerts.append(kwargs) or 1)
    app.recover_last_recording()
    app.clear_history_requested()
    assert calls[-2:] == [module.ADOPTING_STATUS, module.ADOPTING_STATUS] and alerts == []
    app._refresh_recordings_window = lambda: calls.append("recordings refreshed")
    app._adopted()
    assert not app._adopting and calls[-1] == "recordings refreshed"
