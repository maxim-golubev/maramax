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
    from parakeet_dictation import recordings

    store = RecordingStore(tmp_path)

    def fail(*_args):
        raise OSError("disk full")

    monkeypatch.setattr(recordings, "write_text_atomically", fail)
    # The audio is in place, so the caller is told it was saved and does not
    # keep a second copy for recovery.
    saved = store.save(b"\x01\x02" * 16000)
    assert saved is not None
    records = RecordingStore(tmp_path).list_recordings()
    assert [record.id for record in records] == [saved.id]
    assert [path.suffix for path in tmp_path.iterdir()] == [".wav"]
    assert RecordingStore(tmp_path).load_pcm(records[0].id) == b"\x01\x02" * 16000


def test_failed_pruning_does_not_unsave_the_new_recording(tmp_path, monkeypatch):
    store = RecordingStore(tmp_path)

    def fail(_newest_id):
        raise OSError("permission denied")

    monkeypatch.setattr(store, "_prune", fail)
    assert store.save(b"\x01\x02" * 16000) is not None


def test_archive_folder_removed_while_running_is_made_again(tmp_path):
    import shutil

    store = RecordingStore(tmp_path / "recordings")
    shutil.rmtree(tmp_path / "recordings")
    store.clear()  # Nothing left to clear is not an error.
    record = store.save(b"\x01\x02" * 16000)
    assert [entry.id for entry in store.list_recordings()] == [record.id]


def test_writes_interrupted_by_a_crash_are_swept_with_the_next_save(tmp_path):
    store = RecordingStore(tmp_path)
    for suffix in (".wav.tmp", ".json.tmp"):
        (tmp_path / (("e" * 32) + suffix)).write_bytes(b"private audio from a killed save")
    unrelated = tmp_path / "notes.tmp"
    unrelated.write_text("keep")
    record = store.save(b"\x01\x02" * 16000)
    assert sorted(path.name for path in tmp_path.iterdir()) == sorted(
        [f"{record.id}.wav", f"{record.id}.json", "notes.tmp"])


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


def test_metadata_from_a_newer_version_keeps_its_transcript(tmp_path):
    store = RecordingStore(tmp_path)
    record = store.save(b"\x01\x02" * 16000)
    store.update(record.id, status="done", text="Spoken words")
    path = store.audio_path(record.id).with_suffix(".json")
    payload = json.loads(path.read_text())
    payload["speaker"] = "added by a later release"
    path.write_text(json.dumps(payload))
    entry = store.list_recordings()[0]
    assert entry.text == "Spoken words" and entry.status == "done"
    store.update(record.id, message="looked at")
    saved = json.loads(path.read_text())
    assert saved["speaker"] == "added by a later release" and saved["text"] == "Spoken words"


def test_recovery_prefers_audio_that_never_reached_the_recognizer(tmp_path):
    from parakeet_dictation.recordings import recovery_candidate

    store = RecordingStore(tmp_path)
    done = store.save(b"\x01\x00" * 16000)
    store.update(done.id, status="done")
    assert recovery_candidate(store.list_recordings()) is None
    interrupted = store.save(b"\x02\x00" * 16000)
    failed = store.save(b"\x03\x00" * 16000)
    store.update(failed.id, status="failed")
    assert recovery_candidate(store.list_recordings()).id == interrupted.id
    store.update(interrupted.id, status="done")
    assert recovery_candidate(store.list_recordings()).id == failed.id
