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
assert not panel.panel.isVisible()
'''
    subprocess.run([sys.executable, "-c", script, str(tmp_path)], check=True, capture_output=True, text=True, timeout=20)
