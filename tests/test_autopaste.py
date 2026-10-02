"""No real clipboard access, app activation, or keyboard events."""

import threading
from types import SimpleNamespace

import pytest

from parakeet_dictation import app as module
from parakeet_dictation import autopaste


@pytest.fixture
def paste_context(monkeypatch):
    events, scheduled, statuses = [], [], []
    target = SimpleNamespace(
        processIdentifier=lambda: 123,
        isTerminated=lambda: False,
        activateWithOptions_=lambda _options: events.append("activate"),
    )
    front = [target]
    workspace = SimpleNamespace(
        frontmostApplication=lambda: front[0],
        notificationCenter=lambda: SimpleNamespace(
            addObserverForName_object_queue_usingBlock_=lambda *_args: "observer",
            removeObserver_=lambda _observer: None),
    )
    monkeypatch.setattr(module, "accessibility_trusted", lambda: True)
    monkeypatch.setattr(module, "contains_text", lambda text: text == "transcript")
    monkeypatch.setattr(module, "send_paste_keystroke", lambda: events.append("paste"))
    monkeypatch.setattr(module, "call_later", lambda delay, fn: scheduled.append((delay, fn)))
    app = object.__new__(module.DictationApp)
    app._session = 1
    app._shutting_down = False
    app._compact_session = True
    app._previous_app = target
    app._paste_target = autopaste.PasteTarget(workspace=workspace, own_pid=999)
    app._cancel_event = threading.Event()
    app.overlay_visible = False
    app.overlay_controller = SimpleNamespace(hide=lambda: events.append("hide"))
    app._push_status = lambda message, *args, **kwargs: statuses.append(message)
    app._open_accessibility_settings = lambda: events.append("settings")
    return app, front, events, scheduled, statuses


def test_compact_paste_does_not_activate_an_app_or_wait_for_focus(paste_context):
    app, _, events, scheduled, _ = paste_context
    app._paste_into_previous_app_on_main(1, "transcript")
    assert len(scheduled) == 1
    delay, send = scheduled[0]
    assert delay == 0
    send()
    assert events == ["hide", "paste"]


def test_compact_paste_skips_if_user_already_switched_apps(paste_context):
    app, front, events, scheduled, statuses = paste_context
    front[0] = SimpleNamespace(processIdentifier=lambda: 456)
    app._paste_into_previous_app_on_main(1, "transcript")
    assert not scheduled
    assert not events
    assert "focus changed" in statuses[-1]


@pytest.mark.parametrize("change", ["focus", "session", "shutdown", "clipboard", "cancel"])
def test_pending_paste_rechecks_state_before_dispatch(paste_context, monkeypatch, change):
    app, front, events, scheduled, statuses = paste_context
    app._paste_into_previous_app_on_main(1, "transcript")
    if change == "focus":
        front[0] = None
    elif change == "session":
        app._session += 1
    elif change == "shutdown":
        app._shutting_down = True
    elif change == "cancel":
        app._cancel_event.set()
    else:
        monkeypatch.setattr(module, "contains_text", lambda _text: False)
    scheduled[0][1]()
    assert "paste" not in events
    if change == "clipboard":
        assert "clipboard changed" in statuses[-1]


def test_full_window_restores_target_then_rechecks_focus(paste_context):
    app, _, events, scheduled, _ = paste_context
    app._compact_session = False
    app._paste_into_previous_app_on_main(1, "transcript")
    assert events == ["hide", "activate"]
    assert scheduled[0][0] == 0.3
    scheduled[0][1]()
    assert events[-1] == "paste"


def test_closed_target_is_not_activated(paste_context):
    app, _, events, scheduled, statuses = paste_context
    app._compact_session = False
    app._previous_app.isTerminated = lambda: True
    app._paste_into_previous_app_on_main(1, "transcript")
    assert "activate" not in events
    assert not scheduled
    assert "closed" in statuses[-1]


def test_missing_permission_never_posts_events(paste_context, monkeypatch):
    app, _, events, scheduled, statuses = paste_context
    monkeypatch.setattr(module, "accessibility_trusted", lambda: False)
    app._paste_into_previous_app_on_main(1, "transcript")
    assert events == ["settings"]
    assert not scheduled
    assert "Accessibility" in statuses[-1]


@pytest.mark.parametrize("fail_key_up", [False, True])
def test_keyboard_events_are_allocated_before_posting_and_always_released(monkeypatch, fail_key_up):
    events = []

    def create(_source, _key, down):
        events.append(("create", down))
        return 10 if down else (None if fail_key_up else 20)

    monkeypatch.setattr(autopaste, "_core_graphics", SimpleNamespace(
        CGEventCreateKeyboardEvent=create,
        CGEventSetFlags=lambda _event, _flags: None,
        CGEventPost=lambda _tap, event: events.append(("post", event)),
    ))
    monkeypatch.setattr(autopaste, "_core_foundation", SimpleNamespace(
        CFRelease=lambda event: events.append(("release", event)),
    ))
    if fail_key_up:
        with pytest.raises(autopaste.PasteError):
            autopaste.send_paste_keystroke()
        assert events == [("create", True), ("create", False), ("release", 10)]
    else:
        autopaste.send_paste_keystroke()
        assert events == [("create", True), ("create", False), ("post", 10),
                          ("post", 20), ("release", 10), ("release", 20)]


def test_paste_target_is_the_last_app_used_other_than_maramax():
    apps = {name: SimpleNamespace(processIdentifier=lambda pid=pid: pid, name=name)
            for name, pid in (("editor", 1), ("browser", 2), ("maramax", 999))}
    front = [apps["editor"]]
    blocks = []
    workspace = SimpleNamespace(
        frontmostApplication=lambda: front[0],
        notificationCenter=lambda: SimpleNamespace(
            addObserverForName_object_queue_usingBlock_=lambda _name, _obj, _queue, block: blocks.append(block),
            removeObserver_=lambda _observer: None),
    )
    target = autopaste.PasteTarget(workspace=workspace, own_pid=999)
    assert target.current().name == "editor"

    def activate(name):
        front[0] = apps[name]
        blocks[0](SimpleNamespace(userInfo=lambda: {autopaste.NSWorkspaceApplicationKey: apps[name]}))

    activate("maramax")   # Opening a Maramax window is not a new target.
    assert target.current().name == "editor"
    activate("browser")   # The user moved on while the window floated.
    activate("maramax")   # Clicking Record in the window.
    assert target.current().name == "browser"
    assert not target.is_frontmost(target.current())
    assert not target.is_frontmost(None)


def test_cancelled_dictation_is_not_typed_into_another_app(monkeypatch, tmp_path):
    from parakeet_dictation.config import AppConfig
    from parakeet_dictation.history import HistoryStore

    app = object.__new__(module.DictationApp)
    app.config = AppConfig(paste_to_active_app=True)
    app.history_store = HistoryStore(base_dir=tmp_path)
    app._cancel_event = threading.Event()
    app._set_current_text_on_main = lambda *_args: None
    app._refresh_history_on_main = lambda: None
    app._copy_text_with_feedback = lambda text, **kwargs: True
    queued = []
    monkeypatch.setattr(module.AppHelper, "callAfter", lambda *args: queued.append(args))
    app._publish_transcript("words", module.Source.MICROPHONE, "Dictation", 1)
    assert len(queued) == 1
    app._cancel_event.set()  # Cancel arrived too late to stop the transcript itself.
    app._publish_transcript("more words", module.Source.MICROPHONE, "Dictation", 2)
    assert len(queued) == 1
