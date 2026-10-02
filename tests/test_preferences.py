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

NSApplication.sharedApplication().setActivationPolicy_(NSApplicationActivationPolicyProhibited)
path = Path(sys.argv[1]) / "settings.json"
config = AppConfig()
delegate = SimpleNamespace(config=config, transcriber=SimpleNamespace(is_ready=lambda: True, load_error=None))
delegate._save_settings = lambda: config.save(path) or True
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
from parakeet_dictation.recorder import InputDevice
calls = []
delegate._refresh_input_devices = lambda: calls.append("refresh")
delegate.handle_device_selected = lambda name: calls.append(name)
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
delegate.handle_keep_ready_selected = lambda seconds: calls.append(seconds) or setattr(config, "keep_mic_ready_seconds", seconds)
assert panel.keep_ready.titleOfSelectedItem() == "Off"
panel.keep_ready.selectItemWithTitle_("2 minutes")
panel.selectKeepReady_(None)
assert calls[-1] == 120
panel.refresh()
assert panel.keep_ready.titleOfSelectedItem() == "2 minutes"
config.keep_mic_ready_seconds = 45  # Hand-edited settings stay visible instead of snapping to a preset.
panel.refresh()
assert panel.keep_ready.titleOfSelectedItem() == "45 seconds"
assert panel.model_retry.isHidden()
assert set(panel.options) == set(_SETTING_LABELS)
# Every page fits the window: nothing is laid out past the bottom edge.
content = panel.panel.contentView()
content.layoutSubtreeIfNeeded()
for page in panel.pages:
    assert page.frame().origin.y >= 0, page.frame()
assert not panel.panel.isVisible()
'''
    subprocess.run([sys.executable, "-c", script, str(tmp_path)], check=True, capture_output=True, text=True, timeout=20)
