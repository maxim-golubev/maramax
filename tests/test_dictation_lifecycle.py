import threading
from types import SimpleNamespace

from parakeet_dictation import app as module
from parakeet_dictation.capture import CaptureMeter
from parakeet_dictation.config import AppConfig


def controller(monkeypatch):
    app = object.__new__(module.DictationApp)
    calls = []
    app.config = AppConfig()
    app.transcriber = SimpleNamespace(is_ready=lambda: True)
    app.recorder = SimpleNamespace(start=lambda: True, last_error=None,
                                  capture_snapshot=CaptureMeter().snapshot)
    app._state_lock = threading.Lock()
    app._shutting_down = False
    app._overlay_session = 0
    app.recording_active = False
    app.is_transcribing = False
    app.overlay_visible = False
    app._starting = False
    app._start_thread = None
    app._recordings_window = None
    app._capture_previous_app = lambda: None
    app._apply_status_on_main = lambda message, *_args, **_kwargs: calls.append(message)
    app._push_status = lambda message, *_args, **_kwargs: calls.append(message)
    app.overlay_controller = SimpleNamespace(
        prepare_for_recording=lambda: None,
        show_mode=lambda _mode: calls.append("activated window"),
        show_active_microphone=lambda _name: None,
    )
    app.indicator = SimpleNamespace(show=lambda: calls.append("passive bar"),
                                    set_capture=lambda _snapshot: None)
    app.hotkey_manager = SimpleNamespace(set_recording_shortcut=lambda handler: calls.append(handler))
    monkeypatch.setattr(module.AppHelper, "callAfter", lambda *_args: None)
    monkeypatch.setattr(module.AppHelper, "callLater", lambda *_args: None)
    return app, calls


def test_compact_start_returns_while_the_audio_backend_is_still_opening(monkeypatch):
    app, calls = controller(monkeypatch)
    release = threading.Event()
    entered = threading.Event()

    def open_backend():
        entered.set()
        return release.wait(timeout=2)

    app.recorder.start = open_backend
    try:
        assert app.start_recording()
        assert entered.wait(timeout=1)
        assert not release.is_set()
        assert app._starting
        assert "passive bar" in calls
        assert "activated window" not in calls
    finally:
        release.set()
        if app._start_thread is not None:
            app._start_thread.join(timeout=2)


def test_stop_during_startup_is_applied_after_the_backend_returns(monkeypatch):
    app, _ = controller(monkeypatch)
    app.recording_active = True
    app._starting = True
    app._hide_after_transcription = False
    app._force_copy_after_transcription = False
    app._stop_when_started = False
    app.stop_recording_requested(auto_copy=True, hide_after=True)
    assert app._stop_when_started
    assert app._hide_after_transcription
    assert app._force_copy_after_transcription
    stopped = []
    app.stop_recording_requested = lambda: stopped.append(True)
    app._recording_started(True, 0)
    assert stopped == [True]
    assert not app._starting


def test_stale_temporary_hotkey_cannot_stop_a_later_session(monkeypatch):
    app, _ = controller(monkeypatch)
    handlers = []
    stopped = []
    app.hotkey_manager.set_recording_shortcut = handlers.append
    app.stop_recording_requested = lambda: stopped.append(True)
    app._set_recording_shortcut(True)
    app._overlay_session += 1
    handlers[0]()
    assert stopped == []
    app._set_recording_shortcut(False)
    assert handlers[-1] is None


def test_late_start_completion_cannot_start_preview_after_capture_failed(monkeypatch):
    app, _ = controller(monkeypatch)
    app._compact_session = False
    app._stop_when_started = False
    app.recording_active = True
    app._monitor_capture = lambda _session: setattr(app, "recording_active", False)
    previews = []
    app._start_live_preview = lambda: previews.append(True)
    app._recording_started(True, 0)
    assert previews == []


def test_expanding_during_transcription_preserves_original_paste_target(monkeypatch):
    app, _ = controller(monkeypatch)
    app.is_transcribing = True
    app.current_transcript = ""
    app._previous_app = original = object()
    app._capture_previous_app = lambda: setattr(app, "_previous_app", object())
    app.indicator.hide = lambda: None
    app._refresh_input_devices = lambda: None
    app._show_overlay_on_main("result")
    assert app._previous_app is original
    assert app._overlay_session == 0


def test_failed_microphone_start_does_not_leave_connecting_as_the_resting_status(monkeypatch):
    app, calls = controller(monkeypatch)
    app._base_status = "Connecting microphone…"
    app._last_status = ""
    app._compact_session = False
    app._stop_when_started = False
    app.recording_active = True
    app._starting = True
    app.recorder.last_error = RuntimeError("Selected microphone disconnected: AirPods")
    app.recorder.prepare = lambda: calls.append("fresh helper")
    app._recording_started(False, 0)
    assert app._base_status == "Ready"
    assert not app.recording_active and not app._starting
    assert "Selected microphone disconnected" in calls[-2]
    assert calls[-1] == "fresh helper"


def test_audio_helper_failure_ends_the_recording_at_once(monkeypatch):
    app, calls = controller(monkeypatch)
    app._compact_session = True
    app._capture_health = "receiving"
    app._capture_device = "System default"
    app._capture_warning = ""
    app.recording_active = True
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
    app._capture_warning = ""
    app.recording_active = True
    stopped = []
    app.stop_recording_requested = lambda: stopped.append(True)
    app._monitor_capture(0)
    assert stopped == []
    assert app._capture_warning == "Microphone changed to MacBook Pro Microphone during recording"
    assert calls[-1] == "Recording…"


def test_dictation_started_behind_the_queue_dialog_is_not_overrun(monkeypatch):
    app, calls = controller(monkeypatch)
    app.queue = SimpleNamespace(pending_count=lambda: 1)

    def dialog():
        app.recording_active = True  # Option+Space fired inside the modal loop.
        return SimpleNamespace(mode=None)

    app.overlay_controller.show_output_mode_dialog = dialog
    app.queue_start_requested()
    assert not app.is_transcribing
    assert calls[-1] == "Finish the current operation first"


def test_recover_prefers_audio_that_was_never_transcribed(monkeypatch, tmp_path):
    from parakeet_dictation.recordings import RecordingStore

    app, _ = controller(monkeypatch)
    store = RecordingStore(tmp_path)
    interrupted = store.save(b"\x01\x00" * 16000)
    failed = store.save(b"\x02\x00" * 16000)
    store.update(failed.id, status="failed")
    app.recordings = store
    app.recorder.has_recoverable_recording = lambda: False
    app._reset_deferred_flags = lambda: None
    app._cancel_event = threading.Event()
    chosen = []
    app._recover_worker = lambda session, recording_id=None: chosen.append(recording_id)
    app.recover_last_recording()
    app_thread = [t for t in threading.enumerate() if t is not threading.current_thread()]
    for thread in app_thread:
        thread.join(timeout=2)
    assert chosen == [interrupted.id]


def test_crash_leftover_is_moved_into_recordings_once(monkeypatch, tmp_path):
    from parakeet_dictation.recordings import RecordingStore

    app, _ = controller(monkeypatch)
    pcm = b"\x01\x00" * 16000
    leftovers = [pcm]
    app.recordings = RecordingStore(tmp_path)
    app.recorder.load_recoverable_recording = lambda: leftovers[0] if leftovers else None
    app.recorder.discard_recoverable_recording = leftovers.clear
    app._adopt_recovered_audio()
    app._adopt_recovered_audio()
    records = app.recordings.list_recordings()
    assert len(records) == 1
    assert records[0].status == "saved"
    assert "interrupted" in records[0].message
    assert app.recordings.load_pcm(records[0].id) == pcm
