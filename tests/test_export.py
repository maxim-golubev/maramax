import pytest

from parakeet_dictation.export import ExportError, export_results
from parakeet_dictation.queue import OutputConfig, OutputMode, QueueItem


def _make_item(filename="test.mp3", path="/tmp/test.mp3", text="hello world", status="done"):
    return QueueItem(id="abc123", path=path, filename=filename, status=status, result_text=text)


def test_export_clipboard(monkeypatch):
    copied = []
    monkeypatch.setattr("parakeet_dictation.export.copy_text", lambda t: copied.append(t))

    item = _make_item()
    config = OutputConfig(mode=OutputMode.CLIPBOARD)
    result = export_results([item], config)

    assert "1 transcript" in result
    assert copied == ["hello world"]


def test_export_clipboard_multiple(monkeypatch):
    copied = []
    monkeypatch.setattr("parakeet_dictation.export.copy_text", lambda t: copied.append(t))

    items = [
        _make_item(filename="a.mp3", text="first"),
        _make_item(filename="b.mp3", text="second"),
    ]
    config = OutputConfig(mode=OutputMode.CLIPBOARD)
    result = export_results(items, config)

    assert "2 transcripts" in result
    assert "## a.mp3" in copied[0]
    assert "## b.mp3" in copied[0]
    assert "first" in copied[0]
    assert "second" in copied[0]


def test_export_individual_same_dir(tmp_path):
    source = tmp_path / "audio.mp3"
    source.touch()

    item = _make_item(filename="audio.mp3", path=str(source), text="transcribed text")
    config = OutputConfig(mode=OutputMode.INDIVIDUAL_SAME_DIR)
    result = export_results([item], config)

    output = tmp_path / "audio.txt"
    assert output.exists()
    assert output.read_text() == "transcribed text"
    assert "1 file" in result


def test_export_individual_chosen_dir(tmp_path):
    out_dir = tmp_path / "output"
    out_dir.mkdir()

    item = _make_item(filename="audio.mp3", path="/original/audio.mp3", text="transcribed")
    config = OutputConfig(mode=OutputMode.INDIVIDUAL_CHOSEN_DIR, output_path=str(out_dir))
    result = export_results([item], config)

    output = out_dir / "audio.txt"
    assert output.exists()
    assert output.read_text() == "transcribed"


def test_export_individual_handles_existing_file(tmp_path):
    source = tmp_path / "audio.mp3"
    source.touch()
    existing = tmp_path / "audio.txt"
    existing.write_text("old content")

    item = _make_item(filename="audio.mp3", path=str(source), text="new content")
    config = OutputConfig(mode=OutputMode.INDIVIDUAL_SAME_DIR)
    export_results([item], config)

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
    config = OutputConfig(mode=OutputMode.SINGLE_FILE, output_path=str(out_path))
    result = export_results(items, config)

    content = out_path.read_text()
    assert "## a.mp3" in content
    assert "first" in content
    assert "## b.mp3" in content
    assert "second" in content
    assert "combined.txt" in result


def test_export_single_file_single_item(tmp_path):
    out_path = tmp_path / "single.txt"

    item = _make_item(text="only text")
    config = OutputConfig(mode=OutputMode.SINGLE_FILE, output_path=str(out_path))
    export_results([item], config)

    assert out_path.read_text() == "only text"


def test_export_skips_non_done_items(monkeypatch):
    copied = []
    monkeypatch.setattr("parakeet_dictation.export.copy_text", lambda t: copied.append(t))

    items = [
        _make_item(filename="a.mp3", text="good", status="done"),
        _make_item(filename="b.mp3", text="", status="failed"),
        _make_item(filename="c.mp3", text="", status="pending"),
    ]
    config = OutputConfig(mode=OutputMode.CLIPBOARD)
    export_results(items, config)

    assert len(copied) == 1
    assert "good" in copied[0]


def test_export_raises_on_no_completed():
    items = [_make_item(status="failed", text="")]
    config = OutputConfig(mode=OutputMode.CLIPBOARD)

    with pytest.raises(ExportError, match="No completed"):
        export_results(items, config)


def test_export_single_file_no_path():
    item = _make_item()
    config = OutputConfig(mode=OutputMode.SINGLE_FILE, output_path=None)

    with pytest.raises(ExportError, match="No output file"):
        export_results([item], config)


def test_export_individual_chosen_no_path():
    item = _make_item()
    config = OutputConfig(mode=OutputMode.INDIVIDUAL_CHOSEN_DIR, output_path=None)

    with pytest.raises(ExportError, match="No output directory"):
        export_results([item], config)
