import os
from datetime import datetime, timezone

from parakeet_dictation import recovery


def _write_in_progress(tmp_path, size: int, sample: bytes = b"\x01\x02") -> bytes:
    pcm = sample * (size // 2)
    recovery.in_progress_path(tmp_path).write_bytes(pcm)
    return pcm


def _legacy_path(tmp_path):
    return tmp_path / recovery.LEGACY_NAME


def test_promote_missing_file_returns_false(tmp_path):
    assert recovery.promote_in_progress(tmp_path) is False
    assert recovery.unsaved_recordings(tmp_path) == []


def test_promote_too_short_file_deletes_it(tmp_path):
    _write_in_progress(tmp_path, recovery.MIN_RECOVERABLE_BYTES - 2)

    assert recovery.promote_in_progress(tmp_path) is False
    assert not recovery.in_progress_path(tmp_path).exists()
    assert list(tmp_path.iterdir()) == []


def test_promote_moves_recoverable_file(tmp_path):
    pcm = _write_in_progress(tmp_path, recovery.MIN_RECOVERABLE_BYTES)

    assert recovery.promote_in_progress(tmp_path) is True
    assert not recovery.in_progress_path(tmp_path).exists()
    assert [recovery.load_unsaved(path) for path in recovery.unsaved_recordings(tmp_path)] == [pcm]


def test_each_failed_dictation_keeps_its_own_audio_whatever_its_length(tmp_path):
    # A long capture, a shorter one, then a longer one: with one slot, the
    # shorter was deleted and the longest replaced the first.
    kept = []
    for seconds, sample in ((60, b"\x01\x00"), (10, b"\x02\x00"), (90, b"\x03\x00")):
        kept.append(_write_in_progress(tmp_path, recovery.MIN_RECOVERABLE_BYTES * 2 * seconds, sample))
        assert recovery.promote_in_progress(tmp_path) is True
    assert [recovery.load_unsaved(path) for path in recovery.unsaved_recordings(tmp_path)] == kept


def test_a_recording_left_by_an_earlier_version_is_the_oldest(tmp_path):
    legacy = b"\x09\x00" * recovery.MIN_RECOVERABLE_BYTES
    _legacy_path(tmp_path).write_bytes(legacy)
    newer = _write_in_progress(tmp_path, recovery.MIN_RECOVERABLE_BYTES)
    assert recovery.promote_in_progress(tmp_path) is True  # A crash leftover never overwrites it.
    assert [recovery.load_unsaved(path) for path in recovery.unsaved_recordings(tmp_path)] == [legacy, newer]


def test_numbering_continues_after_the_oldest_was_adopted(tmp_path):
    for sample in (b"\x01\x00", b"\x02\x00"):
        _write_in_progress(tmp_path, recovery.MIN_RECOVERABLE_BYTES, sample)
        recovery.promote_in_progress(tmp_path)
    oldest, newest = recovery.unsaved_recordings(tmp_path)
    recovery.discard_unsaved(oldest)
    third = _write_in_progress(tmp_path, recovery.MIN_RECOVERABLE_BYTES, b"\x03\x00")
    recovery.promote_in_progress(tmp_path)
    assert recovery.unsaved_recordings(tmp_path)[0] == newest
    assert recovery.load_unsaved(recovery.unsaved_recordings(tmp_path)[-1]) == third


def test_too_short_and_unrelated_files_are_not_offered(tmp_path):
    _legacy_path(tmp_path).write_bytes(b"x" * 10)
    (tmp_path / "unsaved-recording-x.pcm").write_bytes(b"x" * recovery.MIN_RECOVERABLE_BYTES)
    (tmp_path / "settings.json").write_text("{}")
    assert recovery.unsaved_recordings(tmp_path) == []
    assert recovery.unsaved_recordings(tmp_path / "missing") == []


def test_discard_every_unsaved_leaves_other_files(tmp_path):
    _legacy_path(tmp_path).write_bytes(b"x" * 10)
    _write_in_progress(tmp_path, recovery.MIN_RECOVERABLE_BYTES)
    recovery.promote_in_progress(tmp_path)
    (tmp_path / "settings.json").write_text("{}")
    recovery.discard_every_unsaved(tmp_path)
    assert [path.name for path in tmp_path.iterdir()] == ["settings.json"]


def test_discard_in_progress_is_idempotent(tmp_path):
    recovery.discard_in_progress(tmp_path)  # nothing to remove — no error

    _write_in_progress(tmp_path, recovery.MIN_RECOVERABLE_BYTES)
    recovery.discard_in_progress(tmp_path)
    assert not recovery.in_progress_path(tmp_path).exists()


def test_discard_unsaved_is_idempotent(tmp_path):
    path = _legacy_path(tmp_path)
    recovery.discard_unsaved(path)  # nothing to remove — no error

    path.write_bytes(b"x" * recovery.MIN_RECOVERABLE_BYTES)
    recovery.discard_unsaved(path)
    assert not path.exists()


def test_load_unsaved_missing_or_too_short_returns_none(tmp_path):
    assert recovery.load_unsaved(_legacy_path(tmp_path)) is None
    _legacy_path(tmp_path).write_bytes(b"x" * 10)
    assert recovery.load_unsaved(_legacy_path(tmp_path)) is None


def test_an_unsaved_recording_is_dated_when_its_capture_ended(tmp_path):
    _write_in_progress(tmp_path, recovery.MIN_RECOVERABLE_BYTES)
    ended = datetime(2026, 3, 9, 8, 30, tzinfo=timezone.utc)
    os.utime(recovery.in_progress_path(tmp_path), (ended.timestamp(), ended.timestamp()))
    recovery.promote_in_progress(tmp_path)  # Keeping it is a rename: the date survives.
    assert recovery.captured_at(recovery.unsaved_recordings(tmp_path)[0]) == ended
