import json
import threading

import pytest

from parakeet_dictation.recordings import RecordingStore


def test_audio_survives_an_empty_transcription_and_restart(tmp_path):
    store = RecordingStore(tmp_path)
    pcm = b"\x01\x02" * 16000
    record = store.save(pcm, {"device_name": "AirPods"})
    store.update(record.id, status="failed", message="No transcript returned")

    reopened = RecordingStore(tmp_path)
    entry = reopened.list_recordings()[0]
    assert entry.status == "failed"
    assert entry.diagnostics["device_name"] == "AirPods"
    assert reopened.load_pcm(entry.id) == pcm
    assert entry.duration == 1


def test_silent_audio_is_retained_too(tmp_path):
    store = RecordingStore(tmp_path)
    record = store.save(bytes(32000))
    assert store.load_pcm(record.id) == bytes(32000)


def test_each_recording_survives_later_failed_recordings(tmp_path):
    store = RecordingStore(tmp_path)
    first = store.save(b"\x01\x02" * 16000)
    second = store.save(b"\x03\x04" * 16000)
    store.update(first.id, status="failed")
    store.update(second.id, status="failed")
    assert len(store.list_recordings()) == 2
    assert store.load_pcm(first.id).startswith(b"\x01\x02")


@pytest.mark.parametrize("metadata", [None, "{broken", json.dumps({"id": "../../wrong"})])
def test_wav_is_recoverable_without_usable_metadata(tmp_path, metadata):
    store = RecordingStore(tmp_path)
    record = store.save(b"\x01\x02" * 16000)
    path = store.audio_path(record.id).with_suffix(".json")
    if metadata is None:
        path.unlink()
    else:
        path.write_text(metadata)
    entry = store.list_recordings()[0]
    assert entry.id == record.id
    assert entry.status == "saved"
    assert entry.duration == 1


def test_failed_metadata_write_does_not_remove_audio(tmp_path, monkeypatch):
    store = RecordingStore(tmp_path)

    def fail(_record):
        raise OSError("disk full")

    monkeypatch.setattr(store, "_write_metadata", fail)
    with pytest.raises(OSError):
        store.save(b"\x01\x02" * 16000)
    records = RecordingStore(tmp_path).list_recordings()
    assert len(records) == 1
    assert RecordingStore(tmp_path).load_pcm(records[0].id) == b"\x01\x02" * 16000


def test_retention_count_and_byte_budget(tmp_path):
    store = RecordingStore(tmp_path, limit=2, max_bytes=100000)
    first = store.save(bytes(32000))
    store.save(bytes(32000))
    newest = store.save(bytes(32000))
    assert len(store.list_recordings()) == 2
    assert not store.audio_path(first.id).exists()
    assert store.audio_path(newest.id).exists()

    # Keep a new capture even when it exceeds the whole storage budget.
    large = store.save(bytes(200000))
    assert [r.id for r in store.list_recordings()] == [large.id]


def test_recording_identifiers_cannot_escape_storage(tmp_path):
    store = RecordingStore(tmp_path)
    with pytest.raises(ValueError):
        store.load_pcm("../../history")


def test_empty_capture_has_no_archive(tmp_path):
    store = RecordingStore(tmp_path)
    assert store.save(b"") is None
    assert store.list_recordings() == []


def test_clear_removes_audio_and_transcripts_but_not_unrelated_files(tmp_path):
    store = RecordingStore(tmp_path)
    record = store.save(bytes(32000))
    store.update(record.id, text="Private dictation")
    unrelated = tmp_path / "unrelated.txt"
    unrelated.write_text("keep")
    store.clear()
    assert store.list_recordings() == []
    assert not list(tmp_path.glob("*.json"))
    assert unrelated.exists()


def test_clear_also_removes_damaged_audio_and_interrupted_writes(tmp_path):
    store = RecordingStore(tmp_path)
    for suffix in (".wav", ".json", ".wav.tmp", ".json.tmp"):
        (tmp_path / (("f" * 32) + suffix)).write_bytes(b"damaged but private")
    store.clear()
    assert list(tmp_path.iterdir()) == []


def test_listing_does_not_wait_for_an_in_progress_archive_write(tmp_path, monkeypatch):
    store = RecordingStore(tmp_path)
    entered = threading.Event()
    release = threading.Event()
    finished = threading.Event()
    original = store._write_metadata

    def paused_write(record):
        entered.set()
        release.wait(timeout=2)
        original(record)

    monkeypatch.setattr(store, "_write_metadata", paused_write)

    def save():
        store.save(bytes(32000))
        finished.set()

    writer = threading.Thread(target=save)
    writer.start()
    try:
        assert entered.wait(timeout=1)
        assert len(store.list_recordings()) == 1
        assert not finished.is_set()
    finally:
        release.set()
        writer.join(timeout=3)
