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


# Console tints by severity, most severe first: the lowest level each applies
# to and its ANSI SGR parameters.
_TINTS = (
    (logging.CRITICAL, "1;31"),
    (logging.ERROR, "31"),
    (logging.WARNING, "33"),
    (logging.INFO, "32"),
    (logging.DEBUG, "36"),
)


def _tint(level: int) -> str | None:
    """The SGR parameters for a record of `level`, or None for one below DEBUG."""
    return next((parameters for lowest, parameters in _TINTS if level >= lowest), None)


class ConsoleFormatter(logging.Formatter):
    """A console line: the time, then the level and message, which take the
    severity's tint when `tinted`."""

    def __init__(self, tinted: bool):
        super().__init__("%(levelname)s - %(message)s", datefmt="%H:%M:%S")
        self._tinted = tinted

    def format(self, record: logging.LogRecord) -> str:
        said = super().format(record)
        parameters = _tint(record.levelno) if self._tinted else None
        if parameters is not None:
            said = f"\033[{parameters}m{said}\033[0m"
        return f"{self.formatTime(record, self.datefmt)} - {said}"


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

    wanted = os.environ.get("LOG_LEVEL", "").upper()
    logger.setLevel(logging.getLevelNamesMapping().get(wanted, logging.INFO))

    handler = logging.StreamHandler()
    handler.setFormatter(ConsoleFormatter(tinted="NO_COLOR" not in os.environ))

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
