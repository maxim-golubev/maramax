import pytest

from parakeet_dictation.clipboard import ClipboardError
from parakeet_dictation.export import ExportError, OutputMode, ToFile, ToFolder, export_results
from parakeet_dictation.file_queue import QueuedFile, QueueStatus


def _make_item(filename="test.mp3", path="/tmp/test.mp3", text="hello world", status="done"):
    return QueuedFile(id="abc123", path=path, filename=filename, status=status, result_text=text)


def test_export_clipboard(monkeypatch):
    copied = []
    monkeypatch.setattr("parakeet_dictation.export.copy_text", lambda t: copied.append(t))

    result = export_results([_make_item()], OutputMode.CLIPBOARD)

    assert result == "Copied 1 transcript to clipboard"
    assert copied == ["hello world"]


def test_export_clipboard_multiple(monkeypatch):
    copied = []
    monkeypatch.setattr("parakeet_dictation.export.copy_text", lambda t: copied.append(t))

    items = [
        _make_item(filename="a.mp3", text="first"),
        _make_item(filename="b.mp3", text="second"),
    ]
    result = export_results(items, OutputMode.CLIPBOARD)

    assert result == "Copied 2 transcripts to clipboard"
    assert "## a.mp3" in copied[0]
    assert "## b.mp3" in copied[0]
    assert "first" in copied[0]
    assert "second" in copied[0]


def test_export_next_to_originals(tmp_path):
    source = tmp_path / "audio.mp3"
    source.touch()

    item = _make_item(filename="audio.mp3", path=str(source), text="transcribed text")
    result = export_results([item], OutputMode.NEXT_TO_ORIGINALS)

    output = tmp_path / "audio.txt"
    assert output.read_text() == "transcribed text"
    assert result == "Saved 1 transcript next to its original"


def test_export_next_to_originals_in_several_folders(tmp_path):
    items = []
    for name in ("one", "two"):
        (tmp_path / name).mkdir()
        items.append(_make_item(filename="audio.mp3", path=str(tmp_path / name / "audio.mp3"), text=name))

    result = export_results(items, OutputMode.NEXT_TO_ORIGINALS)

    assert [(tmp_path / name / "audio.txt").read_text() for name in ("one", "two")] == ["one", "two"]
    assert result == "Saved 2 transcripts next to their originals"


def test_export_to_folder_names_the_folder_not_its_whole_path(tmp_path):
    out_dir = tmp_path / "output"
    out_dir.mkdir()

    item = _make_item(filename="audio.mp3", path="/original/audio.mp3", text="transcribed")
    summary = export_results([item], ToFolder(out_dir))

    assert (out_dir / "audio.txt").read_text() == "transcribed"
    assert summary == "Saved 1 transcript to output"


def test_folder_that_cannot_be_made_is_an_export_error_naming_it(tmp_path):
    item = _make_item(filename="audio.mp3", path="/original/audio.mp3", text="transcribed")
    # A file where the output folder should be: mkdir raises OSError.
    blocker = tmp_path / "not_a_dir"
    blocker.write_text("file in the way")

    with pytest.raises(ExportError) as raised:
        export_results([item], ToFolder(blocker / "sub"))

    assert str(raised.value).startswith("could not write audio.txt to sub: ")


def test_a_failed_export_says_so_once_on_the_status_line(tmp_path):
    from parakeet_dictation.app import queue_run_summary

    blocker = tmp_path / "not_a_dir"
    blocker.write_text("file in the way")
    with pytest.raises(ExportError) as raised:
        export_results([_make_item()], ToFile(blocker / "transcript.txt"))

    status = queue_run_summary(cancelled=False, exported=None, export_error=str(raised.value), failures=[])
    assert status.startswith("Export failed: could not write transcript.txt: ")
    assert status.lower().count("failed") == 1


def test_a_refused_clipboard_is_an_export_error(monkeypatch):
    def refuse(_text):
        raise ClipboardError("clipboard copy failed")

    monkeypatch.setattr("parakeet_dictation.export.copy_text", refuse)

    with pytest.raises(ExportError, match="^could not copy to the clipboard$"):
        export_results([_make_item()], OutputMode.CLIPBOARD)


def test_export_individual_handles_existing_file(tmp_path):
    source = tmp_path / "audio.mp3"
    source.touch()
    existing = tmp_path / "audio.txt"
    existing.write_text("old content")

    item = _make_item(filename="audio.mp3", path=str(source), text="new content")
    export_results([item], OutputMode.NEXT_TO_ORIGINALS)

    # Original should be untouched
    assert existing.read_text() == "old content"
    # New file with suffix
    output2 = tmp_path / "audio_2.txt"
    assert output2.exists()
    assert output2.read_text() == "new content"


def test_export_single_file(tmp_path):
    out_path = tmp_path / "combined.txt"

    items = [
        _make_item(filename="a.mp3", text="first"),
        _make_item(filename="b.mp3", text="second"),
    ]
    result = export_results(items, ToFile(out_path))

    content = out_path.read_text()
    assert "## a.mp3" in content
    assert "first" in content
    assert "## b.mp3" in content
    assert "second" in content
    assert result == "Saved 2 transcripts to combined.txt"


def test_export_single_file_single_item(tmp_path):
    out_path = tmp_path / "single.txt"

    export_results([_make_item(text="only text")], ToFile(out_path))

    assert out_path.read_text() == "only text"


def test_export_skips_non_done_items(monkeypatch):
    copied = []
    monkeypatch.setattr("parakeet_dictation.export.copy_text", lambda t: copied.append(t))

    items = [
        _make_item(filename="a.mp3", text="good", status="done"),
        _make_item(filename="b.mp3", text="", status="failed"),
        _make_item(filename="c.mp3", text="", status="pending"),
    ]
    export_results(items, OutputMode.CLIPBOARD)

    assert len(copied) == 1
    assert "good" in copied[0]


def test_export_raises_on_no_completed():
    items = [_make_item(status="failed", text="")]

    with pytest.raises(ExportError, match="no completed"):
        export_results(items, OutputMode.CLIPBOARD)


def test_one_folder_that_cannot_be_written_does_not_keep_the_other_transcripts_from_being_saved(tmp_path):
    locked, open_folder = tmp_path / "locked", tmp_path / "open"
    locked.mkdir()
    open_folder.mkdir()
    locked.chmod(0o500)
    try:
        items = [QueuedFile("a", str(locked / "a.m4a"), "a.m4a", QueueStatus.DONE, "first"),
                 QueuedFile("b", str(open_folder / "b.m4a"), "b.m4a", QueueStatus.DONE, "second")]
        with pytest.raises(ExportError, match=r"could not write a.txt to locked: Permission denied "
                                              r"\(1 of 2 transcripts saved; the rest are in History\)"):
            export_results(items, OutputMode.NEXT_TO_ORIGINALS)
        assert (open_folder / "b.txt").read_text() == "second"
    finally:
        locked.chmod(0o700)
