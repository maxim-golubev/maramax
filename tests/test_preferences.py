"""Exercise native settings in a separate process with all windows hidden."""

import subprocess
import sys


def test_settings_edit_save_reload_and_remove_without_showing_ui(tmp_path):
    script = r'''
import sys
from pathlib import Path
from types import SimpleNamespace
from AppKit import NSApplication, NSApplicationActivationPolicyProhibited
from parakeet_dictation.preferences import PreferencesController
from parakeet_dictation.config import AppConfig
from parakeet_dictation.app import _SETTING_LABELS
from parakeet_dictation.hotkeys import DEFAULT_DICTATE

NSApplication.sharedApplication().setActivationPolicy_(NSApplicationActivationPolicyProhibited)
path = Path(sys.argv[1]) / "settings.json"
config = AppConfig()
delegate = SimpleNamespace(
    config=config, is_busy=False,
    transcriber=SimpleNamespace(load_error=None, status_message=lambda: "Speech model ready"),
    qwen=SimpleNamespace(status_message=lambda: "Loading the high-accuracy model…"),
    updates=SimpleNamespace(status_text=lambda: "This is the newest version.", can_check=lambda: True),
    current_shortcut=lambda: DEFAULT_DICTATE, choose_shortcut=lambda key, modifiers: None,
    pause_shortcut=lambda: None, resume_shortcut=lambda: None,
)
def replace_word_rules(rules):
    config.replacements = rules
    config.save(path)
    return True
delegate.replace_word_rules = replace_word_rules
panel = PreferencesController.alloc().initWithDelegate_labels_(delegate, _SETTING_LABELS)
panel.heard.setStringValue_("mara max")
panel.replacement.setStringValue_("Maramax")
panel.saveRule_(None)
assert AppConfig.load(path).replacements == [{"heard": "mara max", "replacement": "Maramax"}]
panel.selectRule_(None)
panel.heard.setStringValue_("mara macs")
panel.saveRule_(None)
assert AppConfig.load(path).replacements == [{"heard": "mara macs", "replacement": "Maramax"}]
panel.removeRule_(None)
assert not AppConfig.load(path).replacements
# Return in either field saves; Tab to the next field does not.
panel.heard.setStringValue_("mara max")
panel.replacement.setStringValue_("Maramax")
panel.panel.makeFirstResponder_(panel.heard)
panel.panel.fieldEditor_forObject_(True, panel.heard).insertTab_(None)
assert not AppConfig.load(path).replacements
panel.panel.fieldEditor_forObject_(True, panel.replacement).insertNewline_(None)
assert AppConfig.load(path).replacements == [{"heard": "mara max", "replacement": "Maramax"}]
panel.removeRule_(None)
assert not AppConfig.load(path).replacements
from parakeet_dictation.helper_protocol import InputDevice
calls = []
delegate.refresh_input_devices = lambda: calls.append("refresh")
delegate.select_input_device = lambda name: calls.append(name)
assert len(panel.pages) == 3
assert not panel.pages[0].isHidden()
assert panel.pages[1].isHidden()
panel.tabs.setSelectedSegment_(1)
panel.selectTab_(None)
assert calls == ["refresh"]
assert panel.pages[0].isHidden()
assert not panel.pages[1].isHidden()
panel.update_input_devices([InputDevice(1, "AirPods", False)], "AirPods")
panel.selectDevice_(None)
assert calls[-1] == "AirPods"
panel.update_input_devices([], "Disconnected mic")
assert panel.device_picker.titleOfSelectedItem() == "Disconnected mic"
panel.update_input_devices([InputDevice(0, "MacBook Pro Microphone", True)], None, "MacBook Pro Microphone")
assert panel.device_picker.titleOfSelectedItem() == "Automatic — MacBook Pro Microphone"
panel.selectDevice_(None)
assert calls[-1] is None
delegate.is_busy = True
panel.show_busy_state()                  # Told when a dictation starts, not only on a refresh.
assert not panel.device_picker.isEnabled()
delegate.is_busy = False
panel.show_busy_state()
assert panel.device_picker.isEnabled()
delegate.set_keep_microphone_ready = lambda seconds: calls.append(seconds) or setattr(config, "keep_mic_ready_seconds", seconds)
assert panel.keep_ready.titleOfSelectedItem() == "Off"
panel.keep_ready.selectItemWithTitle_("2 minutes")
panel.selectKeepReady_(None)
assert calls[-1] == 120
panel.refresh()
assert panel.keep_ready.titleOfSelectedItem() == "2 minutes"
config.keep_mic_ready_seconds = 45  # Hand-edited settings stay visible instead of snapping to a preset.
panel.refresh()
assert panel.keep_ready.titleOfSelectedItem() == "45 seconds"
config.keep_mic_ready_seconds = 60
panel.refresh()
assert panel.keep_ready.titleOfSelectedItem() == "1 minute"
assert panel.model_retry.isHidden()
assert set(panel.options) == set(_SETTING_LABELS)
# Every tab's window ends the same distance below its content.
from parakeet_dictation.preferences import MARGIN, duration_label, rule_title
content = panel.panel.contentView()
for index, page in enumerate(panel.pages):
    panel.tabs.setSelectedSegment_(index)
    panel.selectTab_(None)
    content.layoutSubtreeIfNeeded()
    assert abs(page.frame().origin.y - MARGIN) < 1, (index, page.frame())
assert [duration_label(n) for n in (0, 1, 30, 60, 120, 90)] == ["Off", "1 second", "30 seconds", "1 minute", "2 minutes", "90 seconds"]
assert len(rule_title({"heard": "sig", "replacement": "line one\nline two " * 40})) == 60
assert "\n" not in rule_title({"heard": "sig", "replacement": "a\nb"})
config.high_accuracy = True
panel.refresh()
assert str(panel.model_status.stringValue()) == "Speech model ready. Loading the high-accuracy model."
# The version is at the top of General; Check Now asks the updater and shows what it says.
from parakeet_dictation import __version__
def labels(view):
    found = [str(view.stringValue())] if hasattr(view, "stringValue") and view.isKindOfClass_(__import__("AppKit").NSTextField) else []
    return found + [text for child in view.subviews() for text in labels(child)]
general = labels(panel.pages[0])
assert general[:2] == ["Maramax", f"Version {__version__}"], general[:3]
# Automatic prefers the Mac's own microphone by default, and macOS shows no
# Accessibility prompt of its own: the help says what really happens.
assert any(text.startswith("Automatic uses the Mac’s own microphone") for text in labels(panel.pages[1]))
assert any("turn Maramax on there" in text for text in general)
assert str(panel.update_status.stringValue()) == "This is the newest version." and panel.update_check.isEnabled()
checks = []
delegate.updates = SimpleNamespace(status_text=lambda: "Checking for updates…", can_check=lambda: False,
                                   check_requested=lambda: checks.append("check"))
panel.checkForUpdates_(None)
assert checks == ["check"]
assert str(panel.update_status.stringValue()) == "Checking for updates…" and not panel.update_check.isEnabled()
# Clicking another app must not make a window of a Dock-less app vanish.
assert not panel.panel.hidesOnDeactivate()
assert not panel.panel.isVisible() and not panel.shows_microphones()
'''
    result = subprocess.run([sys.executable, "-c", script, str(tmp_path)], capture_output=True, text=True, timeout=20)
    assert result.returncode == 0, result.stderr[-1500:]
