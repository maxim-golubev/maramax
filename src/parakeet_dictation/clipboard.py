from __future__ import annotations

import pyperclip


class ClipboardError(RuntimeError):
    pass


def copy_text(text: str) -> None:
    try:
        pyperclip.copy(text)
    except pyperclip.PyperclipException as exc:
        raise ClipboardError("clipboard copy failed") from exc
