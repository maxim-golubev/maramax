import json
from pathlib import Path

from parakeet_dictation.history import HistoryStore


def test_history_store_limits_and_persists_entries(tmp_path):
    store = HistoryStore(history_limit=2, base_dir=tmp_path)

    store.add_entry("microphone", "First", "one")
    store.add_entry("microphone", "Second", "two")
    store.add_entry("file", "Third", "three")

    entries = store.list_entries()
    assert [entry.source_label for entry in entries] == ["Third", "Second"]

    payload = json.loads((tmp_path / "history.json").read_text(encoding="utf-8"))
    assert [item["source_label"] for item in payload] == ["Third", "Second"]


def test_history_store_renders_empty_state(tmp_path):
    store = HistoryStore(base_dir=tmp_path)

    rendered = store.render()

    assert "No transcriptions yet." in rendered
    assert "Option+Space" in rendered


def test_history_store_migrates_legacy_history(tmp_path, monkeypatch):
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
    monkeypatch.setattr(Path, "home", lambda: tmp_path)

    store = HistoryStore()

    assert store.base_dir == support_dir / "Maramax"
    assert (store.base_dir / "history.json").exists()
    assert store.list_entries()[0].source_label == "Legacy"


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
