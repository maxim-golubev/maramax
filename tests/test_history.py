import json

from parakeet_dictation.history import HistoryStore, adopt_legacy_history


def test_history_store_limits_and_persists_entries(tmp_path):
    store = HistoryStore(history_limit=2, base_dir=tmp_path)

    store.add_entry("microphone", "First", "one")
    store.add_entry("microphone", "Second", "two")
    store.add_entry("file", "Third", "three")

    entries = store.list_entries()
    assert [entry.source_label for entry in entries] == ["Third", "Second"]

    payload = json.loads((tmp_path / "history.json").read_text(encoding="utf-8"))
    assert [item["source_label"] for item in payload] == ["Third", "Second"]


def test_empty_history_has_nothing_to_render(tmp_path):
    assert HistoryStore(base_dir=tmp_path).render() is None


def test_history_store_migrates_legacy_history(tmp_path):
    support_dir = tmp_path / "Library" / "Application Support"
    legacy_dir = support_dir / "ParakeetDictation"
    legacy_dir.mkdir(parents=True)
    legacy_history = legacy_dir / "history.json"
    legacy_history.write_text(
        json.dumps(
            [
                {
                    "id": "legacy",
                    "created_at": "2026-03-09T00:00:00+00:00",
                    "source_kind": "microphone",
                    "source_label": "Legacy",
                    "text": "hello",
                }
            ]
        ),
        encoding="utf-8",
    )
    current = support_dir / "Maramax"
    adopt_legacy_history(current)
    store = HistoryStore(base_dir=current)

    assert (current / "history.json").exists()
    assert store.list_entries()[0].source_label == "Legacy"
    store.add_entry("microphone", "New", "words")
    adopt_legacy_history(current)  # Never again: it must not overwrite what exists.
    assert len(HistoryStore(base_dir=current).list_entries()) == 2


def test_malformed_fields_do_not_prevent_loading_valid_history(tmp_path):
    store = HistoryStore(base_dir=tmp_path)
    store.add_entry("microphone", "Valid", "hello")
    payload = json.loads(store.path.read_text())
    payload.insert(0, dict(payload[0], text=42))
    store.path.write_text(json.dumps(payload))
    assert "hello" in HistoryStore(base_dir=tmp_path).render()
    assert len(HistoryStore(base_dir=tmp_path).list_entries()) == 1


def test_failed_history_deletion_is_reported(tmp_path, monkeypatch):
    store = HistoryStore(base_dir=tmp_path)

    def fail():
        raise OSError("disk unavailable")

    monkeypatch.setattr(store, "_save", fail)
    assert not store.clear()


def test_originals_are_retained_without_breaking_older_history_readers(tmp_path):
    store = HistoryStore(base_dir=tmp_path, history_limit=1)
    first = store.add_entry("microphone", "Dictation", "Maramax", raw_text="mara max")
    payload = json.loads(store.path.read_text())
    assert set(payload[0]) == {"id", "created_at", "source_kind", "source_label", "text"}
    assert HistoryStore(base_dir=tmp_path).list_entries()[0].raw_text == "mara max"
    store.add_entry("microphone", "Next", "next")
    assert first.id not in json.loads(store.originals_path.read_text())
    assert store.clear()
    assert json.loads(store.path.read_text()) == []
    assert json.loads(store.originals_path.read_text()) == {}


def test_unreadable_originals_are_set_aside_not_overwritten(tmp_path):
    store = HistoryStore(base_dir=tmp_path)
    store.add_entry("microphone", "Dictation", "Maramax", raw_text="mara max")
    store.originals_path.write_text("{truncated")
    reopened = HistoryStore(base_dir=tmp_path)
    reopened.add_entry("microphone", "Next", "words")
    assert (tmp_path / "history-originals.json.corrupt").read_text() == "{truncated"
    assert json.loads(store.originals_path.read_text()) == {}


def test_history_folder_removed_while_running_is_made_again(tmp_path):
    import shutil

    store = HistoryStore(base_dir=tmp_path / "Maramax")
    shutil.rmtree(tmp_path / "Maramax")
    store.add_entry("microphone", "Next", "words")
    assert len(HistoryStore(base_dir=tmp_path / "Maramax").list_entries()) == 1


def test_clear_leaves_no_transcript_anywhere(tmp_path):
    secret = "my bank PIN is 1234"
    legacy = tmp_path / "ParakeetDictation" / "history.json"
    legacy.parent.mkdir()
    legacy.write_text(json.dumps([{"id": "legacy", "created_at": "2026-03-09T00:00:00+00:00",
                                   "source_kind": "microphone", "source_label": "Legacy", "text": secret}]))
    support = tmp_path / "Maramax"
    adopt_legacy_history(support)
    (support / "history.json.corrupt").write_text(secret)        # Set aside once as unreadable,
    (support / "history.json.corrupt-2").write_text(secret)      # and again,
    (support / "history-originals.json.corrupt").write_text(secret)
    (support / "history.json.tmp").write_text(secret)            # and a write a crash interrupted.
    store = HistoryStore(base_dir=support)
    store.add_entry("microphone", "Dictation", secret, raw_text=secret + " raw")
    unrelated = support / "settings.json"
    unrelated.write_text("{}")
    assert store.clear()
    assert [path for path in tmp_path.rglob("*") if path.is_file() and secret in path.read_text()] == []
    assert unrelated.exists()


def test_unreadable_history_is_set_aside_not_overwritten(tmp_path):
    (tmp_path / "history.json").write_text("")  # What a power loss can leave behind.
    store = HistoryStore(base_dir=tmp_path)
    assert store.list_entries() == []
    store.add_entry("microphone", "Next", "words")
    assert (tmp_path / "history.json.corrupt").exists()
    assert len(json.loads((tmp_path / "history.json").read_text())) == 1


def test_fields_from_a_newer_version_survive_a_save(tmp_path):
    store = HistoryStore(base_dir=tmp_path)
    store.add_entry("microphone", "First", "one")
    payload = json.loads(store.path.read_text())
    payload[0]["language"] = "en"
    store.path.write_text(json.dumps(payload))
    reopened = HistoryStore(base_dir=tmp_path)
    assert reopened.list_entries()[0].text == "one"
    reopened.add_entry("microphone", "Second", "two")
    assert json.loads(store.path.read_text())[1]["language"] == "en"


def test_a_second_unreadable_file_does_not_replace_the_first_one_set_aside(tmp_path):
    for attempt, damage in enumerate(("first", "second"), start=1):
        (tmp_path / "history.json").write_text(damage)
        HistoryStore(base_dir=tmp_path).add_entry("microphone", "Next", "words")
    assert (tmp_path / "history.json.corrupt").read_text() == "first"
    assert (tmp_path / "history.json.corrupt-2").read_text() == "second"


def test_failed_write_leaves_no_temporary_file(tmp_path, monkeypatch):
    from parakeet_dictation import atomic_file

    def fail(_fd):
        raise OSError("I/O error")

    monkeypatch.setattr(atomic_file.os, "fsync", fail)
    (tmp_path / "settings.json").write_text("old")
    try:
        atomic_file.write_text_atomically(tmp_path / "settings.json", "new")
    except OSError:
        pass
    assert sorted(path.name for path in tmp_path.iterdir()) == ["settings.json"]
    assert (tmp_path / "settings.json").read_text() == "old"


def test_unreadable_history_takes_its_original_texts_aside_with_it(tmp_path):
    """The next save would otherwise rewrite the originals with the new entry
    alone, losing the pre-replacement texts of everything set aside."""
    (tmp_path / "history.json").write_text("{not a list")
    (tmp_path / "history-originals.json").write_text('{"a": "mara max"}')
    store = HistoryStore(base_dir=tmp_path)
    store.add_entry("microphone", "Next", "Maramax", raw_text="mara max")
    assert (tmp_path / "history-originals.json.corrupt").read_text() == '{"a": "mara max"}'
