#!/usr/bin/env python3
import argparse
import os
import signal

from .logger_config import setup_logging
from .paths import ensure_runtime_path

os.environ["TOKENIZERS_PARALLELISM"] = "false"
ensure_runtime_path()

logger = setup_logging()


def _get_version() -> str:
    try:
        from importlib.metadata import version

        return version("parakeet-dictation")
    except Exception:
        return "0.2.0"


def _ensure_gui_app() -> None:
    try:
        from AppKit import NSApplication, NSApplicationActivationPolicyAccessory

        NSApplication.sharedApplication().setActivationPolicy_(
            NSApplicationActivationPolicyAccessory
        )
    except Exception:
        pass


def main():
    from .app import DictationApp

    _ensure_gui_app()

    parser = argparse.ArgumentParser(
        description=(
            "Maramax for macOS.\n\n"
            "Open the overlay with Option+Space, toggle recording with Cmd+R, "
            "copy with Cmd+C, and close with Esc."
        )
    )
    parser.add_argument("--version", action="version", version=f"maramax {_get_version()}")
    parser.parse_args()

    app = DictationApp()

    def handle_signal(signum, frame):
        del signum, frame
        os._exit(0)

    signal.signal(signal.SIGINT, handle_signal)
    signal.signal(signal.SIGTERM, handle_signal)

    try:
        app.run()
    finally:
        app.cleanup()


if __name__ == "__main__":
    main()
