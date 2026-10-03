"""Choosing the dictation shortcut, and the welcome window. Native views are built off-screen in a separate process."""
import subprocess
import sys
from types import SimpleNamespace

from parakeet_dictation import app as module
from parakeet_dictation.app import DictationApp, Phase
from parakeet_dictation.config import AppConfig
from parakeet_dictation.hotkeys import (
    DEFAULT_DICTATE, KEY_NAMES, HotKeyError, cmdKey, controlKey, dictation_shortcut, optionKey,
)
from parakeet_dictation.welcome import recording_note, try_it_text

D, E, SPACE = 0x02, 0x0E, 0x31


def controller(monkeypatch, tmp_path, refuse=False):
    app = object.__new__(DictationApp)
    app.config = AppConfig()
    app._settings_path = tmp_path / "settings.json"
    app._dictate = DEFAULT_DICTATE
    app._hotkey_error_message = None
    app._preferences_window = None
    app._welcome_window = None
    app._phase = Phase.IDLE
    app.transcriber = SimpleNamespace(is_ready=lambda: True, status_message=lambda: "Speech model ready")
    shortcuts, statuses = [], []
    app._show_status = lambda message, revert_after=0: statuses.append(message)
    app._push_status = lambda message, revert_after=0: statuses.append(message)

    def set_dictation_shortcut(spec):
        if refuse and spec is not None and spec != DEFAULT_DICTATE:
            raise HotKeyError("taken")
        shortcuts.append(spec.label if spec else None)

    app.hotkey_manager = SimpleNamespace(set_dictation_shortcut=set_dictation_shortcut)
    intro = []
    app.overlay_controller = SimpleNamespace(set_intro_text=intro.append)
    app.history_store = SimpleNamespace(render=lambda: None)
    monkeypatch.setattr(module.AppHelper, "callAfter", lambda function, *args: function(*args))
    # Neither the keyboard layout nor System Settings of the machine running the tests.
    monkeypatch.setattr(module, "layout_key_names", lambda: KEY_NAMES)
    monkeypatch.setattr(module, "macos_shortcuts", lambda: frozenset({(SPACE, cmdKey)}))
    app.overlay_controller.set_history_text = lambda text: intro.append(text)
    return app, shortcuts, intro, statuses


def test_choosing_a_shortcut_registers_saves_and_renames_it_everywhere(monkeypatch, tmp_path):
    app, shortcuts, intro, _ = controller(monkeypatch, tmp_path)
    assert app.choose_shortcut(D, controlKey | optionKey) is None
    assert shortcuts == ["Control+Option+D"] and app.current_shortcut().label == "Control+Option+D"
    assert AppConfig.load(app._settings_path).dictation_shortcut == [D, controlKey | optionKey]
    assert intro == [module.intro_text("Control+Option+D", app.config), module.empty_history_text("Control+Option+D")]


def test_a_shortcut_is_named_for_the_keyboard_layout_in_use(monkeypatch, tmp_path):
    app, shortcuts, _, _ = controller(monkeypatch, tmp_path)
    monkeypatch.setattr(module, "layout_key_names", lambda: KEY_NAMES | {D: "E"})   # Dvorak
    assert app.choose_shortcut(D, controlKey | optionKey) is None
    assert shortcuts == ["Control+Option+E"]


def test_a_shortcut_that_cannot_be_used_is_refused_with_the_reason(monkeypatch, tmp_path):
    app, shortcuts, _, _ = controller(monkeypatch, tmp_path)
    assert "Spotlight" in app.choose_shortcut(SPACE, cmdKey)              # macOS has it turned on.
    assert "types a character" in app.choose_shortcut(E, optionKey)     # Option+E starts é.
    assert shortcuts == [] and app.current_shortcut() == DEFAULT_DICTATE


def test_a_shortcut_maramax_cannot_register_leaves_the_old_one_working(monkeypatch, tmp_path):
    app, shortcuts, _, _ = controller(monkeypatch, tmp_path, refuse=True)
    app.pause_shortcut()                                          # Recording new keys.
    problem = app.choose_shortcut(D, controlKey | optionKey)
    assert problem == "Maramax could not register Control+Option+D. Choose another."
    assert shortcuts == [None, "Option+Space"] and app.current_shortcut() == DEFAULT_DICTATE
    assert not app._settings_path.exists()


def test_choosing_a_shortcut_ends_the_warning_to_choose_another(monkeypatch, tmp_path):
    app, _, _, statuses = controller(monkeypatch, tmp_path)
    app._hotkey_error_message = "Maramax could not register Option+Space — choose another shortcut in Settings"
    assert app.choose_shortcut(D, controlKey | optionKey) is None
    assert statuses == ["Ready"] and app._hotkey_error_message is None
    app.choose_shortcut(SPACE, optionKey)                          # Nothing to take back this time.
    assert statuses == ["Ready"]


def test_a_saved_shortcut_macos_took_since_is_reported_at_launch(monkeypatch, tmp_path):
    app, _, _, statuses = controller(monkeypatch, tmp_path)
    app._dictate = dictation_shortcut(SPACE, cmdKey, KEY_NAMES)
    monkeypatch.setattr(module, "GlobalHotKeyManager",
                        lambda handler: SimpleNamespace(set_dictation_shortcut=lambda spec: None))
    app._register_global_hotkeys()
    assert statuses == ["macOS uses Cmd+Space itself — choose another shortcut in Settings"]
    statuses.clear()
    app._dictate = DEFAULT_DICTATE
    app._hotkey_error_message = None
    app._register_global_hotkeys()
    assert statuses == [] and app._hotkey_error_message is None


def test_the_welcome_is_seen_once(monkeypatch, tmp_path):
    app, _, _, _ = controller(monkeypatch, tmp_path)
    assert AppConfig().onboarded is False
    app.finish_welcome()
    assert AppConfig.load(app._settings_path).onboarded is True


def test_either_welcome_choice_copies_the_transcript(monkeypatch, tmp_path):
    app, _, intro, _ = controller(monkeypatch, tmp_path)
    app.config = AppConfig(auto_copy_to_clipboard=False, paste_to_active_app=True)
    app.choose_delivery(False)                                     # "Copy the transcript"
    saved = AppConfig.load(app._settings_path)
    assert (saved.auto_copy_to_clipboard, saved.paste_to_active_app) == (True, False)
    assert "copied automatically" in intro[-1]
    app.choose_delivery(True)
    assert app.config.paste_to_active_app and "pasted into the app" in intro[-1]


def test_changing_a_setting_rewrites_what_the_window_and_the_welcome_say(monkeypatch, tmp_path):
    app, _, intro, _ = controller(monkeypatch, tmp_path)
    refreshed = []
    app._welcome_window = SimpleNamespace(refresh=lambda: refreshed.append("welcome"))
    app.toggle_setting("auto_copy_to_clipboard")                   # Turned off.
    assert intro == [module.intro_text("Option+Space", app.config)] and "stays here" in intro[0]
    assert refreshed == ["welcome"]


def test_the_window_intro_follows_the_settings():
    assert "Press Option+Space to dictate" in module.intro_text("Option+Space", AppConfig())
    assert "copied automatically" in module.intro_text("Option+Space", AppConfig())
    manual = module.intro_text("Option+Space", AppConfig(auto_start_recording=False))
    assert manual.startswith("Press Cmd+R or Dictate to start") and "Option+Space brings this window back" in manual
    kept = module.intro_text("Option+Space", AppConfig(auto_copy_to_clipboard=False))
    assert "copied" not in kept.split("Turn on")[0] and "Copy the transcript to the clipboard" in kept


def test_try_it_describes_what_these_settings_do():
    assert "Press Option+Space again" in try_it_text("Option+Space", AppConfig())
    assert "ready to paste with Cmd+V" in try_it_text("Option+Space", AppConfig())
    assert "pasted where you are typing" in try_it_text("Option+Space", AppConfig(paste_to_active_app=True))
    kept = try_it_text("Option+Space", AppConfig(auto_copy_to_clipboard=False))
    assert "copied" not in kept and "Open Transcript" in kept
    manual = try_it_text("Option+Space", AppConfig(auto_start_recording=False))
    assert "to open Maramax, then Cmd+R to start" in manual
    assert "small bar" in recording_note(AppConfig())
    for config in (AppConfig(compact_dictation=False), AppConfig(auto_start_recording=False)):
        assert "small bar" not in recording_note(config) and "Maramax window" in recording_note(config)


def test_a_shortcut_edited_by_hand_into_something_unusable_falls_back(tmp_path):
    path = tmp_path / "settings.json"
    path.write_text('{"dictation_shortcut": [2, 256]}')         # Cmd+D: every app's own command.
    assert AppConfig.load(path).dictation_shortcut == [DEFAULT_DICTATE.key_code, DEFAULT_DICTATE.modifiers]
    path.write_text('{"dictation_shortcut": [49, 256]}')        # Cmd+Space: kept, and reported at launch.
    assert AppConfig.load(path).dictation_shortcut == [49, 256]


HEADER = r'''
from types import SimpleNamespace
from AppKit import (NSApplication, NSApplicationActivationPolicyProhibited, NSBackingStoreBuffered, NSEvent,
                    NSEventTypeKeyDown, NSMakeRect, NSNotificationCenter, NSPanel, NSWindowDidResignKeyNotification,
                    NSWindowStyleMaskTitled)
NSApplication.sharedApplication().setActivationPolicy_(NSApplicationActivationPolicyProhibited)
from parakeet_dictation import shortcut_picker
from parakeet_dictation.config import AppConfig
from parakeet_dictation.hotkeys import DEFAULT_DICTATE, KEY_NAMES, dictation_shortcut, macos_problem, shortcut_problem
announced = []
shortcut_picker.NSAccessibilityPostNotificationWithUserInfo = lambda view, name, info: announced.append(
    str(info[shortcut_picker.NSAccessibilityAnnouncementKey]))
calls = []
state = {"shortcut": DEFAULT_DICTATE}
def problem(key, modifiers):
    return shortcut_problem(key, modifiers) or macos_problem(key, modifiers, {(0x31, 1 << 8)})
def choose(key, modifiers):
    calls.append(("choose", key, modifiers))
    if problem(key, modifiers) is None:
        state["shortcut"] = dictation_shortcut(key, modifiers, KEY_NAMES)
    return problem(key, modifiers)
owner = SimpleNamespace(
    config=AppConfig(), current_shortcut=lambda: state["shortcut"], choose_shortcut=choose,
    problem_with_shortcut=problem,
    pause_shortcut=lambda: calls.append("pause"), resume_shortcut=lambda: calls.append("resume"),
    transcriber=SimpleNamespace(status_message=lambda: "Speech model ready"),
    paste_permitted=lambda: False, open_accessibility_settings=lambda: calls.append("accessibility"),
    finish_welcome=lambda: calls.append("finished"),
)
def choose_delivery(paste):
    owner.config.auto_copy_to_clipboard = True
    owner.config.paste_to_active_app = paste
    calls.append(("paste", paste))
owner.choose_delivery = choose_delivery
def hidden_window():
    return NSPanel.alloc().initWithContentRect_styleMask_backing_defer_(
        NSMakeRect(0, 0, 400, 200), NSWindowStyleMaskTitled, NSBackingStoreBuffered, False)
def key_down(window, key_code, flags):
    return NSEvent.keyEventWithType_location_modifierFlags_timestamp_windowNumber_context_characters_charactersIgnoringModifiers_isARepeat_keyCode_(
        NSEventTypeKeyDown, (0, 0), flags, 0, window.windowNumber(), None, "d", "d", False, key_code)
'''


def run(body):
    result = subprocess.run([sys.executable, "-c", HEADER + body], capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr[-1500:]


def test_the_picker_offers_presets_and_records_a_shortcut():
    run(r'''
from parakeet_dictation.shortcut_picker import HINT, OTHER, ShortcutPicker
resized = []
picker = ShortcutPicker.alloc().initWithOwner_width_onResize_(owner, 400, lambda: resized.append(1))
window = hidden_window()
window.contentView().addSubview_(picker.view)
titles = [str(t) for t in picker.popup.itemTitles()]
assert titles[0] == "Option+Space (recommended)" and titles[-1] == OTHER, titles
assert str(picker.popup.accessibilityLabel()) == "Dictation shortcut"
assert HINT in [str(view.stringValue()) for view in picker.view.arrangedSubviews()[1:]]
picker.popup.selectItemAtIndex_(1)
picker.chooseItem_(None)                                     # A preset.
assert calls[-1] == ("choose", 0x31, (1 << 12) | (1 << 9)), calls
picker.popup.selectItemAtIndex_(picker.popup.numberOfItems() - 1)
picker.chooseItem_(None)                                     # Other shortcut…
assert calls[-1] == "pause" and picker.is_recording() and not picker.popup.isEnabled()
assert announced[-1] == "Press the keys you want, or Esc to cancel." and resized
picker.key_pressed(0x31, 1 << 20)                            # Cmd+Space: refused, still recording.
assert picker.is_recording() and "Spotlight" in str(picker.note.stringValue())
assert "Spotlight" in announced[-1]                          # VoiceOver hears the refusal too.
picker.key_pressed(0x0E, 1 << 19)                            # Option+E: types é.
assert picker.is_recording() and "types a character" in str(picker.note.stringValue())
picker.key_pressed(0x02, (1 << 18) | (1 << 19))              # Control+Option+D
assert not picker.is_recording() and calls[-1] == ("choose", 0x02, (1 << 12) | (1 << 11))
assert str(picker.popup.titleOfSelectedItem()) == "Control+Option+D"   # A choice that is not a preset is listed.
picker.start_recording()
picker.key_pressed(0x35, 0)                                  # Esc
assert calls[-1] == "resume" and not picker.is_recording()
picker.stop_recording()                                      # Nothing to stop: no second resume.
assert calls.count("resume") == 1
''')


def test_recording_keys_ends_when_its_window_is_left_and_takes_only_its_own_keys():
    run(r'''
from parakeet_dictation.shortcut_picker import ShortcutPicker
picker = ShortcutPicker.alloc().initWithOwner_width_onResize_(owner, 400, lambda: None)
window, other = hidden_window(), hidden_window()
window.contentView().addSubview_(picker.view)
picker.start_recording()
NSApplication.sharedApplication().sendEvent_(key_down(other, 0x02, (1 << 18) | (1 << 19)))
assert picker.is_recording() and not [c for c in calls if c[0] == "choose"], calls   # Typing elsewhere is typing.
NSApplication.sharedApplication().sendEvent_(key_down(window, 0x02, (1 << 18) | (1 << 19)))
assert not picker.is_recording() and calls[-1] == ("choose", 0x02, (1 << 12) | (1 << 11)), calls
picker.start_recording()
NSNotificationCenter.defaultCenter().postNotificationName_object_(NSWindowDidResignKeyNotification, other)
assert picker.is_recording()                                 # Another window resigning changes nothing.
NSNotificationCenter.defaultCenter().postNotificationName_object_(NSWindowDidResignKeyNotification, window)
assert not picker.is_recording() and calls[-1] == "resume"   # Another window or app took the keys.
# A second picker in another window can record once the first window is no longer key.
second = ShortcutPicker.alloc().initWithOwner_width_onResize_(owner, 400, lambda: None)
other.contentView().addSubview_(second.view)
picker.start_recording()
NSNotificationCenter.defaultCenter().postNotificationName_object_(NSWindowDidResignKeyNotification, window)
second.start_recording()
assert not picker.is_recording() and second.is_recording() and calls[-3:] == ["pause", "resume", "pause"], calls
second.stop_recording()
assert calls[-1] == "resume"                                 # Never left paused.
''')


def test_the_welcome_walks_four_steps_and_never_leaves_the_shortcut_paused():
    run(r'''
from parakeet_dictation.welcome import STEPS, WelcomeController, recording_note, try_it_text
welcome = WelcomeController.alloc().initWithDelegate_(owner)
assert not welcome.panel.isVisible()
assert str(welcome.counter.stringValue()) == "1 of 4" and welcome.back.isHidden()
assert "Speech model ready" in str(welcome.model_status.stringValue())
welcome.goForward_(None)
assert not welcome.pages[1].isHidden() and welcome.pages[0].isHidden()
welcome.picker.start_recording()
welcome.goForward_(None)                                     # Leaving the step ends the recording.
assert not welcome.picker.is_recording() and calls[-1] == "resume"
assert welcome.copy_choice.state() == 1 and welcome.permission_row.isHidden()
welcome.choosePaste_(welcome.paste_choice)
assert calls[-1] == ("paste", True) and not welcome.permission_row.isHidden() and not welcome.permission.isHidden()
welcome.choosePaste_(welcome.copy_choice)
assert calls[-1] == ("paste", False) and welcome.permission_row.isHidden() and welcome.copy_choice.state() == 1
owner.config.auto_copy_to_clipboard = False                  # Turned off in Settings: neither choice is true now.
welcome.refresh()
assert welcome.copy_choice.state() == 0 and welcome.paste_choice.state() == 0
welcome.choosePaste_(welcome.paste_choice)
welcome.openAccessibility_(None)
assert calls[-1] == "accessibility"
welcome.goForward_(None)
assert str(welcome.forward.title()) == "Done" and STEPS == 4
assert str(welcome.try_text.stringValue()) == try_it_text("Option+Space", owner.config)
assert str(welcome.recording_note.stringValue()) == recording_note(owner.config)
welcome.picker.start_recording()
welcome.windowWillClose_(None)                               # Closing early still restores the shortcut.
assert calls[-2:] == ["resume", "finished"], calls
''')


def test_done_closes_the_welcome_and_counts_it_as_seen():
    run(r'''
from parakeet_dictation.welcome import STEPS, WelcomeController
welcome = WelcomeController.alloc().initWithDelegate_(owner)
welcome.show_step(STEPS - 1)
welcome.goForward_(None)
assert calls == ["finished"], calls
''')


def test_opening_the_welcome_again_keeps_one_model_watch():
    run(r'''
from parakeet_dictation import welcome as module
scheduled = []
module.call_later = lambda delay, function, *args: scheduled.append((function, args))
welcome = module.WelcomeController.alloc().initWithDelegate_(owner)
welcome.panel = SimpleNamespace(isVisible=lambda: True)     # As if on screen; nothing is shown.
for _ in range(2):                                           # What show() does each time.
    welcome._watch_generation += 1
    welcome._watch_model(welcome._watch_generation)
assert len(scheduled) == 2
for function, args in scheduled[:]:
    function(*args)                                          # One second later.
assert len(scheduled) == 3 and scheduled[-1][1] == (2,)      # The older watch ended.
''')
