"""Choosing the dictation shortcut, and the welcome window. Native views are built off-screen in a separate process."""
import subprocess
import sys
from types import SimpleNamespace

from parakeet_dictation import app as module
from parakeet_dictation.app import DictationApp
from parakeet_dictation.config import AppConfig
from parakeet_dictation.hotkeys import DEFAULT_DICTATE, HotKeyError, controlKey, optionKey

D, SPACE = 0x02, 0x31


def controller(monkeypatch, tmp_path, refuse=False):
    app = object.__new__(DictationApp)
    app.config = AppConfig()
    app._settings_path = tmp_path / "settings.json"
    app._dictate = DEFAULT_DICTATE
    app._hotkey_error_message = None
    app._preferences_window = None
    app._welcome_window = None
    shortcuts = []

    def set_dictation_shortcut(spec):
        if refuse and spec is not None and spec != DEFAULT_DICTATE:
            raise HotKeyError("taken")
        shortcuts.append(spec.label if spec else None)

    app.hotkey_manager = SimpleNamespace(set_dictation_shortcut=set_dictation_shortcut)
    intro = []
    app.overlay_controller = SimpleNamespace(set_intro_text=intro.append)
    app.history_store = SimpleNamespace(render=lambda: None)
    monkeypatch.setattr(module.AppHelper, "callAfter", lambda function, *args: function(*args))
    app.overlay_controller.set_history_text = lambda text: intro.append(text)
    return app, shortcuts, intro


def test_choosing_a_shortcut_registers_saves_and_renames_it_everywhere(monkeypatch, tmp_path):
    app, shortcuts, intro = controller(monkeypatch, tmp_path)
    assert app.choose_shortcut(D, controlKey | optionKey) is None
    assert shortcuts == ["Control+Option+D"] and app.current_shortcut().label == "Control+Option+D"
    assert AppConfig.load(app._settings_path).dictation_shortcut == [D, controlKey | optionKey]
    assert all("Control+Option+D" in text for text in intro)


def test_a_shortcut_that_cannot_be_used_is_refused_with_the_reason(monkeypatch, tmp_path):
    app, shortcuts, _ = controller(monkeypatch, tmp_path)
    assert "Spotlight" in app.choose_shortcut(SPACE, 1 << 8)
    assert shortcuts == [] and app.current_shortcut() == DEFAULT_DICTATE


def test_a_shortcut_another_app_holds_leaves_the_old_one_working(monkeypatch, tmp_path):
    app, shortcuts, _ = controller(monkeypatch, tmp_path, refuse=True)
    app.pause_shortcut()                                          # Recording new keys.
    assert "already used" in app.choose_shortcut(D, controlKey | optionKey)
    assert shortcuts == [None, "Option+Space"] and app.current_shortcut() == DEFAULT_DICTATE
    assert not app._settings_path.exists()


def test_the_welcome_is_seen_once(monkeypatch, tmp_path):
    app, _, _ = controller(monkeypatch, tmp_path)
    assert AppConfig().onboarded is False
    app.finish_welcome()
    assert AppConfig.load(app._settings_path).onboarded is True


def test_a_shortcut_edited_by_hand_into_something_unusable_falls_back(tmp_path):
    path = tmp_path / "settings.json"
    path.write_text('{"dictation_shortcut": [49, 256]}')        # Cmd+Space: Spotlight's.
    assert AppConfig.load(path).dictation_shortcut == [DEFAULT_DICTATE.key_code, DEFAULT_DICTATE.modifiers]


HEADER = r'''
from types import SimpleNamespace
from AppKit import NSApplication, NSApplicationActivationPolicyProhibited
NSApplication.sharedApplication().setActivationPolicy_(NSApplicationActivationPolicyProhibited)
from parakeet_dictation.config import AppConfig
from parakeet_dictation.hotkeys import DEFAULT_DICTATE, dictation_shortcut, shortcut_problem
calls = []
state = {"shortcut": DEFAULT_DICTATE}
def choose(key, modifiers):
    calls.append(("choose", key, modifiers))
    problem = shortcut_problem(key, modifiers)
    if problem is None:
        state["shortcut"] = dictation_shortcut(key, modifiers)
    return problem
owner = SimpleNamespace(
    config=AppConfig(), current_shortcut=lambda: state["shortcut"], choose_shortcut=choose,
    pause_shortcut=lambda: calls.append("pause"), resume_shortcut=lambda: calls.append("resume"),
    transcriber=SimpleNamespace(status_message=lambda: "Speech model ready"),
    paste_permitted=lambda: False, open_accessibility_settings=lambda: calls.append("accessibility"),
    finish_welcome=lambda: calls.append("finished"),
)
def set_paste(enabled):
    owner.config.paste_to_active_app = enabled
    calls.append(("paste", enabled))
owner.set_paste_into_apps = set_paste
'''


def run(body):
    result = subprocess.run([sys.executable, "-c", HEADER + body], capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr[-1500:]


def test_the_picker_offers_presets_and_records_a_shortcut():
    run(r'''
from parakeet_dictation.shortcut_picker import OTHER, ShortcutPicker
picker = ShortcutPicker.alloc().initWithOwner_width_(owner, 400)
titles = [str(t) for t in picker.popup.itemTitles()]
assert titles[0] == "Option+Space (recommended)" and titles[-1] == OTHER, titles
picker.popup.selectItemAtIndex_(1)
picker.chooseItem_(None)                                     # A preset.
assert calls[-1] == ("choose", 0x31, (1 << 12) | (1 << 9)), calls
picker.start_recording()
assert calls[-1] == "pause" and picker.is_recording() and not picker.popup.isEnabled()
picker.key_pressed(0x31, 1 << 20)                            # Cmd+Space: refused, still recording.
assert picker.is_recording() and "Spotlight" in str(picker.note.stringValue())
picker.key_pressed(0x02, (1 << 18) | (1 << 19))              # Control+Option+D
assert not picker.is_recording() and calls[-1] == ("choose", 0x02, (1 << 12) | (1 << 11))
assert str(picker.popup.titleOfSelectedItem()) == "Control+Option+D"   # A choice that is not a preset is listed.
picker.start_recording()
picker.key_pressed(0x35, 0)                                  # Esc
assert calls[-1] == "resume" and not picker.is_recording()
picker.stop_recording()                                      # Nothing to stop: no second resume.
assert calls.count("resume") == 1
''')


def test_the_welcome_walks_four_steps_and_never_leaves_the_shortcut_paused():
    run(r'''
from parakeet_dictation.welcome import STEPS, WelcomeController, try_it_text
welcome = WelcomeController.alloc().initWithDelegate_(owner)
assert not welcome.panel.isVisible()
assert str(welcome.counter.stringValue()) == "1 of 4" and welcome.back.isHidden()
assert "Speech model ready" in str(welcome.model_status.stringValue())
welcome.goForward_(None)
assert not welcome.pages[1].isHidden() and welcome.pages[0].isHidden()
welcome.picker.start_recording()
welcome.goForward_(None)                                     # Leaving the step ends the recording.
assert not welcome.picker.is_recording() and calls[-1] == "resume"
welcome.choosePaste_(welcome.paste_choice)
assert ("paste", True) in calls and not welcome.permission.isHidden()
welcome.openAccessibility_(None)
assert calls[-1] == "accessibility"
welcome.goForward_(None)
assert str(welcome.forward.title()) == "Done" and STEPS == 4
assert str(welcome.try_text.stringValue()) == try_it_text("Option+Space", True)
assert "pasted where you are typing" in try_it_text("Option+Space", True)
welcome.picker.start_recording()
welcome.windowWillClose_(None)                               # Closing early still restores the shortcut.
assert calls[-2:] == ["resume", "finished"], calls
''')
