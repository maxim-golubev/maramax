#!/usr/bin/env python3
"""Process entry point: starts the app or, with --audio-worker, the audio helper."""

import argparse
import os
import signal

from . import __version__
from .instance import InstanceLock
from .logger_config import logger, setup_logging
from .paths import app_support_dir, ensure_runtime_path, ensure_ssl_certs

# Must match CFBundleIdentifier in packaging/setup.py.
BUNDLE_ID = "com.maramax.dictation"
ALREADY_RUNNING = ("Maramax is already running",
                   "Use the Maramax icon in the menu bar, or quit that copy before opening another.")


def _ensure_gui_app() -> None:
    from AppKit import NSApplication, NSApplicationActivationPolicyAccessory

    NSApplication.sharedApplication().setActivationPolicy_(NSApplicationActivationPolicyAccessory)


def main():
    parser = argparse.ArgumentParser(
        description=(
            "Maramax for macOS.\n\n"
            "Press your dictation shortcut (Option+Space unless you chose another) to start dictating "
            "and again, or Cmd+R, to finish. "
            "Where the transcript goes is set under Settings → General: pasted into the app you are using, "
            "copied to the clipboard, or kept in Maramax only."
        )
    )
    parser.add_argument("--version", action="version", version=f"maramax {__version__}")
    parser.add_argument("--audio-worker", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.audio_worker:
        # The helper needs none of the app's environment or GUI. Its own
        # logging goes to stderr, which the app reads (audio_worker.main).
        from .audio_worker import main as audio_main
        audio_main()
        return

    # Before anything imports the speech stack: these shape how it loads.
    os.environ["TOKENIZERS_PARALLELISM"] = "false"
    ensure_runtime_path()
    ensure_ssl_certs()
    setup_logging()

    _ensure_gui_app()
    from AppKit import NSRunningApplication
    import rumps
    from PyObjCTools import MachSignals

    # Also recognize older installed versions that predate the instance lock.
    # One that is exiting (the copy an update just replaced) does not count.
    others = NSRunningApplication.runningApplicationsWithBundleIdentifier_(BUNDLE_ID)
    if any(app.processIdentifier() != os.getpid() and not app.isTerminated() for app in others):
        rumps.alert(title=ALREADY_RUNNING[0], message=ALREADY_RUNNING[1])
        return

    support_dir = app_support_dir()
    lock = InstanceLock(support_dir / "app.lock")
    try:
        acquired = lock.acquire()
    except OSError:
        rumps.alert(title="Maramax cannot open its local storage", message=(
            "Check available disk space and access to Library/Application Support/Maramax inside your home folder."
        ))
        return
    if not acquired:
        rumps.alert(title=ALREADY_RUNNING[0], message=ALREADY_RUNNING[1])
        return

    app = None
    try:
        setup_logging(support_dir / "logs" / "maramax.log")
        logger.info(f"Starting Maramax {__version__}")
        from .app import DictationApp

        app = DictationApp(support_dir=support_dir)
        # Quit from the menu, AppleScript, logout, or a signal all end in
        # applicationWillTerminate, so the microphone is released one way.
        rumps.events.before_quit.register(app.cleanup)

        def quit_on_signal(_signum):
            rumps.quit_application()

        # A plain signal.signal handler only runs when Python next executes
        # on the main thread, which an idle menu-bar app may not do for
        # hours; MachSignals delivers through the run loop.
        MachSignals.signal(signal.SIGINT, quit_on_signal)
        MachSignals.signal(signal.SIGTERM, quit_on_signal)
        app.run()
    except Exception:
        logger.exception("Maramax could not start or stopped unexpectedly")
        rumps.alert(title="Maramax could not start", message=(
            "Try opening the app again. Diagnostic details are saved in "
            "Library/Application Support/Maramax/logs inside your home folder."
        ))
    finally:
        if app is not None:
            app.cleanup()  # Idempotent; reached only when the run loop returned or failed.
        lock.close()


if __name__ == "__main__":
    main()
