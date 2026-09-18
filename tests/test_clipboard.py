import pyperclip
import pytest

from parakeet_dictation import clipboard


def test_copy_text_wraps_pyperclip_errors(monkeypatch):
    def fail(_text: str) -> None:
        raise pyperclip.PyperclipException("no clipboard")

    monkeypatch.setattr(clipboard.pyperclip, "copy", fail)

    with pytest.raises(clipboard.ClipboardError, match="clipboard copy failed"):
        clipboard.copy_text("hello")


def test_contains_text_requires_the_exact_transcript(monkeypatch):
    monkeypatch.setattr(clipboard.pyperclip, "paste", lambda: "new clipboard content")
    assert not clipboard.contains_text("transcript")
    assert clipboard.contains_text("new clipboard content")


def test_contains_text_fails_closed_if_clipboard_cannot_be_read(monkeypatch):
    def fail():
        raise pyperclip.PyperclipException("unavailable")

    monkeypatch.setattr(clipboard.pyperclip, "paste", fail)
    assert not clipboard.contains_text("transcript")
