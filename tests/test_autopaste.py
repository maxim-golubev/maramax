"""No real clipboard access, app activation, or keyboard events."""

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
    workspace = SimpleNamespace(frontmostApplication=lambda: front[0])
    monkeypatch.setattr(module, "NSWorkspace", SimpleNamespace(sharedWorkspace=lambda: workspace))
    monkeypatch.setattr(module, "accessibility_trusted", lambda: True)
    monkeypatch.setattr(module, "contains_text", lambda text: text == "transcript")
    monkeypatch.setattr(module, "send_paste_keystroke", lambda: events.append("paste"))
    monkeypatch.setattr(module.AppHelper, "callLater", lambda delay, fn: scheduled.append((delay, fn)))
    app = object.__new__(module.DictationApp)
    app._overlay_session = 1
    app._shutting_down = False
    app._compact_session = True
    app._previous_app = target
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


@pytest.mark.parametrize("change", ["focus", "session", "shutdown", "clipboard"])
def test_pending_paste_rechecks_state_before_dispatch(paste_context, monkeypatch, change):
    app, front, events, scheduled, statuses = paste_context
    app._paste_into_previous_app_on_main(1, "transcript")
    if change == "focus":
        front[0] = None
    elif change == "session":
        app._overlay_session += 1
    elif change == "shutdown":
        app._shutting_down = True
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
