from parakeet_dictation import recovery


def _write_in_progress(tmp_path, size: int) -> None:
    recovery.in_progress_path(tmp_path).write_bytes(b"\x01\x02" * (size // 2))


def test_promote_missing_file_returns_false(tmp_path):
    assert recovery.promote_in_progress(tmp_path) is False
    assert not recovery.last_recording_path(tmp_path).exists()


def test_promote_too_short_file_deletes_it(tmp_path):
    _write_in_progress(tmp_path, recovery.MIN_RECOVERABLE_BYTES - 2)

    assert recovery.promote_in_progress(tmp_path) is False
    assert not recovery.in_progress_path(tmp_path).exists()
    assert not recovery.last_recording_path(tmp_path).exists()


def test_promote_moves_recoverable_file(tmp_path):
    _write_in_progress(tmp_path, recovery.MIN_RECOVERABLE_BYTES)

    assert recovery.promote_in_progress(tmp_path) is True
    assert not recovery.in_progress_path(tmp_path).exists()
    assert recovery.last_recording_path(tmp_path).exists()


def test_promote_overwrites_previous_last_recording(tmp_path):
    recovery.last_recording_path(tmp_path).write_bytes(b"old" * recovery.MIN_RECOVERABLE_BYTES)
    _write_in_progress(tmp_path, recovery.MIN_RECOVERABLE_BYTES)

    assert recovery.promote_in_progress(tmp_path) is True
    assert recovery.last_recording_path(tmp_path).read_bytes().startswith(b"\x01\x02")


def test_promote_only_if_larger_keeps_longer_existing_recording(tmp_path):
    existing = b"L" * (recovery.MIN_RECOVERABLE_BYTES * 4)
    recovery.last_recording_path(tmp_path).write_bytes(existing)
    _write_in_progress(tmp_path, recovery.MIN_RECOVERABLE_BYTES)

    assert recovery.promote_in_progress(tmp_path, only_if_larger=True) is False
    # The shorter capture is dropped so it can't resurface at next launch;
    # the longer preserved recording survives untouched.
    assert not recovery.in_progress_path(tmp_path).exists()
    assert recovery.last_recording_path(tmp_path).read_bytes() == existing


def test_promote_only_if_larger_replaces_shorter_existing_recording(tmp_path):
    recovery.last_recording_path(tmp_path).write_bytes(b"S" * recovery.MIN_RECOVERABLE_BYTES)
    _write_in_progress(tmp_path, recovery.MIN_RECOVERABLE_BYTES * 4)

    assert recovery.promote_in_progress(tmp_path, only_if_larger=True) is True
    assert recovery.last_recording_path(tmp_path).read_bytes().startswith(b"\x01\x02")


def test_promote_only_if_larger_with_no_existing_recording(tmp_path):
    _write_in_progress(tmp_path, recovery.MIN_RECOVERABLE_BYTES)

    assert recovery.promote_in_progress(tmp_path, only_if_larger=True) is True
    assert recovery.last_recording_path(tmp_path).exists()


def test_has_last_recording(tmp_path):
    assert recovery.has_last_recording(tmp_path) is False

    recovery.last_recording_path(tmp_path).write_bytes(b"x" * 10)
    assert recovery.has_last_recording(tmp_path) is False

    recovery.last_recording_path(tmp_path).write_bytes(b"x" * recovery.MIN_RECOVERABLE_BYTES)
    assert recovery.has_last_recording(tmp_path) is True


def test_discard_in_progress_is_idempotent(tmp_path):
    recovery.discard_in_progress(tmp_path)  # nothing to remove — no error

    _write_in_progress(tmp_path, recovery.MIN_RECOVERABLE_BYTES)
    recovery.discard_in_progress(tmp_path)
    assert not recovery.in_progress_path(tmp_path).exists()


def test_discard_last_recording_is_idempotent(tmp_path):
    recovery.discard_last_recording(tmp_path)  # nothing to remove — no error

    recovery.last_recording_path(tmp_path).write_bytes(b"x" * recovery.MIN_RECOVERABLE_BYTES)
    recovery.discard_last_recording(tmp_path)
    assert not recovery.last_recording_path(tmp_path).exists()


def test_load_last_recording_missing_returns_none(tmp_path):
    assert recovery.load_last_recording(tmp_path) is None


def test_load_last_recording_too_short_returns_none(tmp_path):
    recovery.last_recording_path(tmp_path).write_bytes(b"x" * 10)
    assert recovery.load_last_recording(tmp_path) is None


def test_load_last_recording_returns_bytes(tmp_path):
    payload = b"\x03\x04" * recovery.MIN_RECOVERABLE_BYTES
    recovery.last_recording_path(tmp_path).write_bytes(payload)
    assert recovery.load_last_recording(tmp_path) == payload
