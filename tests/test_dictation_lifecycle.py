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
