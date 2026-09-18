"""Clipboard helpers, including the fail-closed check that guards auto-paste."""

from __future__ import annotations

import pyperclip


class ClipboardError(RuntimeError):
    pass


def copy_text(text: str) -> None:
    try:
        pyperclip.copy(text)
    except pyperclip.PyperclipException as exc:
        raise ClipboardError("clipboard copy failed") from exc


def contains_text(text: str) -> bool:
    """Fail closed if another app has replaced the text before auto-paste."""
    try:
        return pyperclip.paste() == text
    except pyperclip.PyperclipException:
        return False
