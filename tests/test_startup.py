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
from parakeet_dictation.config import AppConfig

NSApplication.sharedApplication().setActivationPolicy_(NSApplicationActivationPolicyProhibited)
base = Path(sys.argv[1])
transcriber = SimpleNamespace(is_ready=lambda: True, load_error=None, status_message=lambda: "Speech model ready")
registered, scheduled = [], []
class Shortcuts:  # Carbon stays out of it: what would be registered is recorded.
    def __init__(self, handler):
        pass
    def set_dictation_shortcut(self, spec):
        registered.append(spec.label)
    def cleanup(self):
        pass
from parakeet_dictation.hotkeys import KEY_NAMES, controlKey, optionKey
with patch.object(module, "ParakeetTranscriber", lambda: transcriber), \
     patch.object(module.DictationApp, "_start_model_watchdog", lambda self: None), \
     patch.object(module, "GlobalHotKeyManager", Shortcuts), \
     patch.object(module, "layout_key_names", lambda: KEY_NAMES), \
     patch.object(module, "macos_shortcuts", lambda: frozenset()), \
     patch.object(module, "call_later", lambda delay, function, *args: scheduled.append(function.__name__)):
    # Everything the app stores goes under the directory it is given.
    app = module.DictationApp(config=AppConfig(dictation_shortcut=[0x02, controlKey | optionKey], onboarded=True),
                              support_dir=base)
    # The shortcut the user chose is the one registered and named, and a
    # user who has seen the welcome does not get it again.
    assert registered == ["Control+Option+D"], registered
    assert "Press Control+Option+D to dictate" in app.overlay_controller.intro_text
    assert "show_welcome" not in scheduled
    import rumps
    for bind in getattr(rumps.clicked, "*buttons", []):
        bind(app)
    assert app.menu["Start Dictation"].callback is not None
    assert app.menu["Recordings…"].callback is not None
    assert app.menu["More"]["History"].callback is not None
    assert "pyaudio" not in sys.modules  # The audio driver lives in the helper process only.
    from parakeet_dictation.preferences import PreferencesController
    prefs = PreferencesController.alloc().initWithDelegate_labels_(app, module._SETTING_LABELS)
    app._preferences_window = prefs
    prefs.toggleSetting_(prefs.options["use_corrections"])
    assert not AppConfig.load(base / "settings.json").use_corrections
    assert not app.config.use_corrections
    assert not app.overlay_controller.panel.isVisible()
    assert not app.indicator.panel.isVisible()
    assert not prefs.panel.isVisible()
    # Text fields in a menu-bar app need an Edit menu for Cmd+V and friends.
    edit = NSApplication.sharedApplication().mainMenu().itemAtIndex_(0).submenu()
    assert {str(edit.itemAtIndex_(i).keyEquivalent()) for i in range(edit.numberOfItems())} >= set("zxcva")
    # The audio helper is launched only once the model is ready, never by construction.
    assert app.recorder._process is None
    assert (app.recorder.device_name, app.recorder.prefer_builtin, app.recorder.keep_warm_seconds) == (None, True, 0)
    assert app.indicator.stop_button.frame().size == app.indicator.expand_button.frame().size
    assert not app.is_busy and app.overlay_controller.intro_text
    statuses = []
    app._push_status = lambda message, revert_after=0: statuses.append(message)
    app.copy_current_transcript()
    assert statuses == ["No transcript to copy"]  # The intro text is guidance, not a transcript.
    app.cleanup()
    app.cleanup()
    assert sorted(path.name for path in base.iterdir()) == ["recordings", "settings.json"]
    # A first launch opens the welcome a moment after start-up.
    import tempfile
    first = module.DictationApp(config=AppConfig(), support_dir=Path(tempfile.mkdtemp(dir=base.parent)))
    assert scheduled.count("show_welcome") == 1 and registered[-1] == "Option+Space"
    first.cleanup()
'''
    result = subprocess.run([sys.executable, "-c", script, str(tmp_path)], capture_output=True, text=True, timeout=20)
    assert result.returncode == 0, result.stderr[-1500:]


def test_importing_the_package_modules_has_no_side_effects(tmp_path):
    script = r'''
import logging, os, sys
before = dict(os.environ)
import parakeet_dictation.main, parakeet_dictation.transcription, parakeet_dictation.recordings
import parakeet_dictation.history, parakeet_dictation.hotkeys, parakeet_dictation.recovery
import parakeet_dictation.isolated_recorder, parakeet_dictation.capture, parakeet_dictation.corrections
assert dict(os.environ) == before, "importing must not change the environment"
assert not logging.getLogger("maramax").handlers, "logging is configured by main(), not by imports"
'''
    subprocess.run([sys.executable, "-c", script], check=True, capture_output=True, text=True, timeout=60, cwd=tmp_path)
