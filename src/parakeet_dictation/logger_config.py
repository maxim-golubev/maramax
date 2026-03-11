from __future__ import annotations

import logging
import os

from dotenv import load_dotenv


_LOGGER_CONFIGURED = False
_LOGGER_NAME = "maramax"


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


def setup_logging() -> logging.Logger:
    global _LOGGER_CONFIGURED

    logger = logging.getLogger(_LOGGER_NAME)

    if _LOGGER_CONFIGURED:
        return logger

    load_dotenv()

    log_level = getattr(logging, os.getenv("LOG_LEVEL", "INFO").upper(), logging.INFO)
    logger.setLevel(log_level)

    handler = logging.StreamHandler()
    handler.setFormatter(ColoredFormatter("%(asctime)s - %(levelname)s - %(message)s", datefmt="%H:%M:%S"))

    logger.handlers.clear()
    logger.addHandler(handler)
    _LOGGER_CONFIGURED = True
    return logger
