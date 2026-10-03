"""Console and rotating-file logging for the shared maramax logger."""

from __future__ import annotations

import logging
import os
from logging.handlers import RotatingFileHandler
from pathlib import Path


_LOGGER_CONFIGURED = False
_LOGGER_NAME = "maramax"

# Modules import this; it stays silent until main() calls setup_logging(), so
# importing a module never reads the environment or opens a log file.
logger = logging.getLogger(_LOGGER_NAME)


class ColoredFormatter(logging.Formatter):
    COLORS = {
        "DEBUG": "\033[36m",
        "INFO": "\033[32m",
        "WARNING": "\033[33m",
        "ERROR": "\033[31m",
        "CRITICAL": "\033[1;31m",
    }
    RESET = "\033[0m"

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.use_colors = "NO_COLOR" not in os.environ

    def format(self, record: logging.LogRecord) -> str:
        if not self.use_colors:
            return super().format(record)

        original_level = record.levelname
        level_color = self.COLORS.get(original_level, "")
        if not level_color:
            return super().format(record)

        try:
            record.levelname = f"{level_color}{original_level}{self.RESET}"
            log_message = super().format(record)
        finally:
            record.levelname = original_level

        parts = log_message.split(" - ", 2)
        if len(parts) < 3:
            return log_message

        timestamp, level, message = parts[0], parts[1], parts[2]
        return f"{timestamp} - {level} - {level_color}{message}{self.RESET}"


def setup_logging(log_path: Path | None = None) -> logging.Logger:
    global _LOGGER_CONFIGURED

    if log_path is not None and not any(isinstance(h, RotatingFileHandler) for h in logger.handlers):
        try:
            log_path.parent.mkdir(parents=True, exist_ok=True)
            file_handler = RotatingFileHandler(log_path, maxBytes=2 * 1024 * 1024, backupCount=2, encoding="utf-8")
            file_handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
            logger.addHandler(file_handler)
        except OSError as exc:
            # A log file that cannot be opened must not stop the app from
            # starting: the console still gets every message, this one included.
            logger.warning(f"Could not open diagnostic log {log_path}: {exc}; logging to the console only")

    if _LOGGER_CONFIGURED:
        return logger

    log_level = getattr(logging, os.getenv("LOG_LEVEL", "INFO").upper(), logging.INFO)
    logger.setLevel(log_level)

    handler = logging.StreamHandler()
    handler.setFormatter(ColoredFormatter("%(asctime)s - %(levelname)s - %(message)s", datefmt="%H:%M:%S"))

    logger.addHandler(handler)
    _LOGGER_CONFIGURED = True
    return logger


def setup_helper_logging() -> logging.Logger:
    """The audio helper's logging: warnings and errors as plain lines on
    stderr, which the app copies into its own log. The helper opens no log
    file of its own: two processes rotating one file would race."""
    handler = logging.StreamHandler()  # stderr
    handler.setFormatter(logging.Formatter("%(levelname)s %(message)s"))
    logger.addHandler(handler)
    logger.setLevel(logging.WARNING)
    return logger
