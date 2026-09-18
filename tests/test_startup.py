"""Startup orchestration without model downloads, hotkeys, or audio devices."""

import subprocess
import sys
import tomllib
from pathlib import Path

from parakeet_dictation import __version__


def test_release_version_matches_project_metadata():
    root = Path(__file__).resolve().parents[1]
    assert tomllib.loads((root / "pyproject.toml").read_text())["project"]["version"] == __version__


def test_version_exits_before_importing_the_gui():
    script = '''
import sys
from parakeet_dictation.main import main
sys.argv = ["maramax", "--version"]
try:
    main()
except SystemExit as exc:
    assert exc.code == 0
assert "parakeet_dictation.app" not in sys.modules
assert "AppKit" not in sys.modules
'''
    result = subprocess.run([sys.executable, "-c", script], check=True, capture_output=True, text=True, timeout=10)
    assert result.stdout.strip() == f"maramax {__version__}"


def test_native_app_construction_and_settings_without_launching(tmp_path):
    script = r'''
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from AppKit import NSApplication, NSApplicationActivationPolicyProhibited
from parakeet_dictation import app as module
from parakeet_dictation import recorder as recorder_module
from parakeet_dictation import isolated_recorder as isolated_module
from parakeet_dictation.config import AppConfig
from parakeet_dictation.history import HistoryStore
from parakeet_dictation.preferences import PreferencesController

NSApplication.sharedApplication().setActivationPolicy_(NSApplicationActivationPolicyProhibited)
base = Path(sys.argv[1])
def forbid_audio():
    raise AssertionError("Startup must not initialize PortAudio")

with patch.object(module, "app_support_dir", lambda: base), \
     patch.object(isolated_module, "app_support_dir", lambda: base), \
     patch.object(module, "HistoryStore", lambda **kw: HistoryStore(base_dir=base, **kw)), \
     patch.object(module, "ParakeetTranscriber", lambda: SimpleNamespace(is_ready=lambda: True, load_error=None)), \
     patch.object(module.DictationApp, "_start_model_watchdog", lambda self: None), \
     patch.object(module.DictationApp, "_register_global_hotkeys", lambda self: None), \
     patch.object(recorder_module.pyaudio, "PyAudio", forbid_audio):
    app = module.DictationApp(config=AppConfig())
    import rumps
    for bind in getattr(rumps.clicked, "*buttons", []):
        bind(app)
    assert app.menu["Start Dictation"].callback is not None
    assert app.menu["Recordings…"].callback is not None
    assert app.menu["More"]["History"].callback is not None
    assert "Toggle Recording" not in app.menu
    assert "Recordings & Recovery…" not in app.menu
    prefs = PreferencesController.alloc().initWithDelegate_labels_(app, module._SETTING_LABELS)
    app._preferences_window = prefs
    prefs.toggleSetting_(prefs.options["use_corrections"])
    assert not AppConfig.load(base / "settings.json").use_corrections
    assert not app.config.use_corrections
    assert not app.overlay_controller.panel.isVisible()
    assert not app.indicator.panel.isVisible()
    assert not prefs.panel.isVisible()
    app.cleanup()
    app.cleanup()
'''
    subprocess.run([sys.executable, "-c", script, str(tmp_path)], check=True, capture_output=True, text=True, timeout=20)
