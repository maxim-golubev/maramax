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
    monkeypatch.setattr(module, "send_paste_keystroke", lambda lead: events.append("paste" + lead))
    monkeypatch.setattr(module, "text_before_cursor", lambda: None)   # An app that does not say.
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
    assert statuses[-1] == module.SWITCHED_APPS_STATUS


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
    if change == "focus":
        assert statuses[-1] == module.SWITCHED_APPS_STATUS


def test_full_window_restores_target_then_rechecks_focus(paste_context):
    app, _, events, scheduled, _ = paste_context
    app._compact_session = False
    app._paste_into_previous_app_on_main(1, "transcript")
    assert events == ["hide", "activate"]
    assert scheduled[0][0] == 0.3
    scheduled[0][1]()
    assert events[-1] == "paste"


@pytest.mark.parametrize("compact", [True, False])
def test_closed_target_is_named_and_leaves_the_window_open(paste_context, compact):
    """Whichever window was used, the reason is that the app quit, and a
    window that cannot be pasted from is not taken away."""
    app, _, events, scheduled, statuses = paste_context
    app._compact_session = compact
    app._previous_app.isTerminated = lambda: True
    app._paste_into_previous_app_on_main(1, "transcript")
    assert events == [] and not scheduled
    assert statuses[-1] == "Copied, not pasted — that app has quit"


def test_missing_permission_never_posts_events_or_opens_a_window(paste_context, monkeypatch):
    """Asking for the permission is Settings' job: a dictation only says so."""
    app, _, events, scheduled, statuses = paste_context
    monkeypatch.setattr(module, "accessibility_trusted", lambda: False)
    app._paste_into_previous_app_on_main(1, "transcript")
    assert events == [] and not scheduled
    assert statuses[-1] == module.NOT_PERMITTED_STATUS


@pytest.mark.parametrize("before, pasted", [("", "paste"), (".", "paste "), ("d", "paste "), (" ", "paste"),
                                            ("(", "paste"), (None, "paste")])
def test_a_transcript_pasted_after_text_is_spaced_from_it(paste_context, monkeypatch, before, pasted):
    """Two dictations in a row read "end. Start", not "end.Start"; the clipboard is not touched."""
    app, _, events, scheduled, _ = paste_context
    monkeypatch.setattr(module, "text_before_cursor", lambda: before)
    app._paste_into_previous_app_on_main(1, "transcript")
    scheduled[0][1]()
    assert events[-1] == pasted


@pytest.mark.parametrize("fail_key_up", [False, True])
def test_keyboard_events_are_allocated_before_posting_and_always_released(monkeypatch, fail_key_up):
    events = []

    def create(_source, key, down):
        assert key == 0x2F  # The key that gives V on this (Dvorak) layout, not the US V key.
        events.append(("create", down))
        return 10 if down else (None if fail_key_up else 20)

    monkeypatch.setattr(autopaste, "command_key_code", {"v": 0x2F}.get)
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


def test_a_leading_space_is_typed_before_cmd_v_and_every_event_exists_before_any_is_posted(monkeypatch):
    events, typed, created = [], [], iter([1, 2, 3, 4])

    def create(_source, key, down):
        number = next(created)
        events.append(("create", key, down))
        return number

    monkeypatch.setattr(autopaste, "command_key_code", {"v": 0x09}.get)
    monkeypatch.setattr(autopaste, "_core_graphics", SimpleNamespace(
        CGEventCreateKeyboardEvent=create,
        CGEventSetFlags=lambda event, flags: events.append(("flags", event, flags)),
        CGEventKeyboardSetUnicodeString=lambda event, length, characters: typed.append(
            (event, "".join(chr(unit) for unit in characters[:length]))),
        CGEventPost=lambda _tap, event: events.append(("post", event)),
    ))
    monkeypatch.setattr(autopaste, "_core_foundation", SimpleNamespace(CFRelease=lambda event: None))
    autopaste.send_paste_keystroke(" ")
    assert typed == [(1, " "), (2, " ")]
    posts = [entry for entry in events if entry[0] == "post"]
    assert posts == [("post", 1), ("post", 2), ("post", 3), ("post", 4)]       # Space, then Cmd+V.
    assert events.index(("post", 1)) > max(index for index, entry in enumerate(events) if entry[0] == "create")
    assert ("flags", 1, 0) in events and ("flags", 3, autopaste.kCGEventFlagMaskCommand) in events


@pytest.mark.parametrize("previous, spaced", [(None, False), ("", False), (" ", False), ("\n", False), ("(", False),
                                              ("“", False), ("a", True), (".", True), (",", True), ("7", True),
                                              (")", True), ("d ", False), ("d.", True), ("x(", False),
                                              ('"', False), ('s"', True), ('."', True), (' "', False), ('("', False),
                                              ("'", False), ("s'", True), ("\n'", False), ("😀", True)])
def test_a_space_goes_in_only_after_a_word_or_punctuation(previous, spaced):
    assert autopaste.space_before(previous) is spaced


def test_no_keystroke_when_no_key_gives_cmd_v(monkeypatch):
    monkeypatch.setattr(autopaste, "command_key_code", lambda character: None)
    monkeypatch.setattr(autopaste, "_core_graphics", None)   # Nothing may be created or posted.
    with pytest.raises(autopaste.PasteError, match="Cmd\\+V"):
        autopaste.send_paste_keystroke()


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
    app.history_store = HistoryStore(base_dir=tmp_path, history_limit=app.config.history_limit)
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


def test_asking_for_permission_clears_an_entry_for_another_build_then_prompts(monkeypatch):
    calls = []
    monkeypatch.setattr(autopaste.subprocess, "run", lambda command, **kwargs: calls.append(command)
                        or SimpleNamespace(returncode=0, stderr=""))
    monkeypatch.setattr(autopaste, "_app_services", SimpleNamespace(
        AXIsProcessTrustedWithOptions=lambda options: calls.append("prompt") or False))
    autopaste.request_accessibility("com.maramax.dictation")
    assert calls == [["/usr/bin/tccutil", "reset", "Accessibility", "com.maramax.dictation"], "prompt"]
    calls.clear()
    autopaste.request_accessibility(None)        # From source: Python's or the terminal's entry is left alone.
    assert calls == ["prompt"]


def test_a_tccutil_that_hangs_or_fails_still_leads_to_the_prompt(monkeypatch):
    calls = []

    def hang(command, **kwargs):
        raise autopaste.subprocess.TimeoutExpired(command, kwargs["timeout"])
    monkeypatch.setattr(autopaste.subprocess, "run", hang)
    monkeypatch.setattr(autopaste, "_app_services", SimpleNamespace(
        AXIsProcessTrustedWithOptions=lambda options: calls.append("prompt") or False))
    autopaste.request_accessibility("com.maramax.dictation")
    assert calls == ["prompt"]


def test_choosing_paste_asks_for_permission_only_when_it_is_missing(monkeypatch, tmp_path):
    from parakeet_dictation.config import AppConfig, Delivery

    app = object.__new__(module.DictationApp)
    app.config = AppConfig()
    app._settings_path = tmp_path / "settings.json"
    app._shutting_down = False
    app._permission_watch = 0
    events = []
    app._show_settings_changed = lambda: events.append("views")
    monkeypatch.setattr(module, "bundle_identifier", lambda: "com.maramax.dictation")
    monkeypatch.setattr(module, "request_accessibility", lambda bundle: events.append(("ask", bundle)))
    monkeypatch.setattr(module, "call_later", lambda delay, fn, *args: events.append(("look again", delay)))
    trusted = [False]
    monkeypatch.setattr(module, "accessibility_trusted", lambda: trusted[0])
    app.choose_delivery(Delivery.PASTED)
    assert AppConfig.load(app._settings_path).delivery() is Delivery.PASTED
    assert events == [("ask", "com.maramax.dictation"), ("look again", 1.0), "views"]
    events.clear()
    app.choose_delivery(Delivery.COPIED)
    assert events == ["views"]
    trusted[0] = True
    app.choose_delivery(Delivery.PASTED)
    assert events == ["views", "views"]


def test_the_permission_watch_tells_the_views_once_it_is_granted(monkeypatch):
    app = object.__new__(module.DictationApp)
    app._shutting_down = False
    app._permission_watch = 1
    events = []
    app._show_settings_changed = lambda: events.append("views")
    app._push_status = lambda message, revert_after=0: events.append(message)
    trusted = [False]
    monkeypatch.setattr(module, "accessibility_trusted", lambda: trusted[0])
    monkeypatch.setattr(module, "call_later", lambda delay, fn, *args: events.append("look again"))
    app._watch_paste_permission(1, deadline=float("inf"))
    assert events == ["look again"]
    trusted[0] = True
    app._watch_paste_permission(1, deadline=float("inf"))
    assert events[1:] == ["views", "Maramax can now paste into the app you are using"]
    events.clear()
    app._watch_paste_permission(0, deadline=float("inf"))   # A newer request took over.
    assert events == []


def test_asking_again_while_already_allowed_never_resets_the_grant(monkeypatch):
    """A Settings window that had not caught up still offers Allow…; a click must not undo the grant."""
    app = object.__new__(module.DictationApp)
    events = []
    app._show_settings_changed = lambda: events.append("views")
    monkeypatch.setattr(module, "accessibility_trusted", lambda: True)
    monkeypatch.setattr(module, "request_accessibility", lambda bundle: events.append("reset and ask"))
    app.request_paste_permission()
    assert events == ["views"]


def test_a_paste_skipped_after_the_window_went_is_said_on_the_bar(paste_context):
    """With the window gone, the menu's status line alone would go unseen."""
    app, front, events, scheduled, statuses = paste_context
    app._compact_session = False
    app._dictate = SimpleNamespace(label="Option+Space")
    app.config = SimpleNamespace(bar_position=[0.5, 0.5])
    app.indicator = SimpleNamespace(show=lambda label, placement: events.append(("bar", placement)),
                                    finish=lambda message, seconds: events.append(("bar says", message)))
    app._paste_into_previous_app_on_main(1, "transcript")
    front[0] = SimpleNamespace(processIdentifier=lambda: 456)          # The user went elsewhere meanwhile.
    scheduled[0][1]()
    assert events[-2:] == [("bar", [0.5, 0.5]), ("bar says", module.SWITCHED_APPS_STATUS)]
    assert statuses[-1] == module.SWITCHED_APPS_STATUS and app._compact_session
