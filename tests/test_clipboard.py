import pyperclip
import pytest

from parakeet_dictation import clipboard


def test_copy_text_wraps_pyperclip_errors(monkeypatch):
    def fail(_text: str) -> None:
        raise pyperclip.PyperclipException("no clipboard")

    monkeypatch.setattr(clipboard.pyperclip, "copy", fail)

    with pytest.raises(clipboard.ClipboardError, match="clipboard copy failed"):
        clipboard.copy_text("hello")
