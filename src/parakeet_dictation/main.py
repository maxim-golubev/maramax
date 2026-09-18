#!/usr/bin/env python3
"""Entry point: CLI flags, single-instance guard, and audio-helper dispatch."""

import argparse
import os
import signal

from .logger_config import setup_logging
from .paths import app_support_dir, ensure_runtime_path, ensure_ssl_certs
from . import __version__
from .instance import InstanceLock

os.environ["TOKENIZERS_PARALLELISM"] = "false"
ensure_runtime_path()
ensure_ssl_certs()

logger = setup_logging()


def _get_version() -> str:
    return __version__


def _ensure_gui_app() -> None:
    try:
        from AppKit import NSApplication, NSApplicationActivationPolicyAccessory

        NSApplication.sharedApplication().setActivationPolicy_(
            NSApplicationActivationPolicyAccessory
        )
    except Exception:
        pass


def main():
    parser = argparse.ArgumentParser(
        description=(
            "Maramax for macOS.\n\n"
            "Press Option+Space to start dictating and again, or Cmd+R, to finish. "
            "The transcript is copied to the clipboard; enable Paste Into Active App in Settings for insertion."
        )
    )
    parser.add_argument("--version", action="version", version=f"maramax {_get_version()}")
    parser.add_argument("--audio-worker", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.audio_worker:
        from .audio_worker import main as audio_main
        audio_main()
        return

    _ensure_gui_app()
    from AppKit import NSRunningApplication
    import rumps
    from PyObjCTools import AppHelper

    # Also recognize older installed versions that predate the instance lock.
    others = NSRunningApplication.runningApplicationsWithBundleIdentifier_("com.maramax.dictation")
    if any(app.processIdentifier() != os.getpid() for app in others):
        rumps.alert(title="Maramax is already running", message="Quit the other copy from its menu bar before opening this version.")
        return

    lock = InstanceLock(app_support_dir() / "app.lock")
    try:
        acquired = lock.acquire()
    except OSError:
        rumps.alert(title="Maramax cannot open its local storage", message=(
            "Check available disk space and access to Library/Application Support/Maramax inside your home folder."
        ))
        return
    if not acquired:
        rumps.alert(title="Maramax is already running", message="Use the Maramax menu bar icon, or quit that copy before opening another.")
        return

    app = None
    try:
        setup_logging(app_support_dir() / "logs" / "maramax.log")
        logger.info(f"Starting Maramax {__version__}")
        from .app import DictationApp

        app = DictationApp()

        def handle_signal(signum, frame):
            del signum, frame
            AppHelper.callAfter(rumps.quit_application)

        signal.signal(signal.SIGINT, handle_signal)
        signal.signal(signal.SIGTERM, handle_signal)
        app.run()
    except Exception:
        logger.exception("Maramax could not start or stopped unexpectedly")
        rumps.alert(title="Maramax could not start", message=(
            "Try opening the app again. Diagnostic details are saved in "
            "Library/Application Support/Maramax/logs inside your home folder."
        ))
    finally:
        if app is not None:
            app.cleanup()
        lock.close()


if __name__ == "__main__":
    main()
