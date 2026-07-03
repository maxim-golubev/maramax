"""Crash-safe recording recovery.

While the mic is recording, AudioRecorder spills the raw PCM stream to an
in-progress file. If transcription succeeds the file is discarded; if the
app hangs, crashes, or transcription fails, the audio survives on disk and
can be transcribed later via "Recover Last Recording".

Files (raw 16-bit mono 16kHz PCM, no header):
- recording-in-progress.pcm  written live during a recording
- last-recording.pcm         the most recent recoverable capture
"""

from __future__ import annotations

from pathlib import Path

from .logger_config import setup_logging

logger = setup_logging()

IN_PROGRESS_NAME = "recording-in-progress.pcm"
LAST_RECORDING_NAME = "last-recording.pcm"

# Below ~0.5s of 16-bit 16kHz mono audio there is nothing worth recovering.
MIN_RECOVERABLE_BYTES = 16000


def in_progress_path(base_dir: Path) -> Path:
    return base_dir / IN_PROGRESS_NAME


def last_recording_path(base_dir: Path) -> Path:
    return base_dir / LAST_RECORDING_NAME


def _size_of(path: Path) -> int:
    """Size in bytes, 0 when missing/unreadable."""
    try:
        return path.stat().st_size
    except OSError:
        return 0


def promote_in_progress(base_dir: Path, only_if_larger: bool = False) -> bool:
    """Keep the in-progress capture as the recoverable last recording.

    Returns True when a recoverable file is now in place. Too-short files
    are deleted rather than promoted so they don't resurface at next launch.

    With only_if_larger (used for deliberate cancels), a capture that is not
    longer than the existing last recording is discarded instead — a quick
    cancelled dictation must not clobber a long recording that is still
    awaiting recovery.
    """
    src = in_progress_path(base_dir)
    size = _size_of(src)
    if size == 0:
        return False
    try:
        if size < MIN_RECOVERABLE_BYTES:
            src.unlink(missing_ok=True)
            return False
        if only_if_larger and size <= _size_of(last_recording_path(base_dir)):
            src.unlink(missing_ok=True)
            return False
        src.replace(last_recording_path(base_dir))
        return True
    except OSError as exc:
        logger.warning(f"Could not preserve recording for recovery: {exc}")
        return False


def _discard(path: Path, what: str) -> None:
    try:
        path.unlink(missing_ok=True)
    except OSError as exc:
        logger.warning(f"Could not remove {what}: {exc}")


def discard_in_progress(base_dir: Path) -> None:
    _discard(in_progress_path(base_dir), "in-progress recording")


def discard_last_recording(base_dir: Path) -> None:
    _discard(last_recording_path(base_dir), "recovered recording")


def has_last_recording(base_dir: Path) -> bool:
    return _size_of(last_recording_path(base_dir)) >= MIN_RECOVERABLE_BYTES


def load_last_recording(base_dir: Path) -> bytes | None:
    """Raw PCM of the recoverable recording, or None when there isn't one.

    Reads the whole file into memory — call from a worker thread, not the
    main thread (an hour of audio is ~115 MB).
    """
    path = last_recording_path(base_dir)
    if _size_of(path) < MIN_RECOVERABLE_BYTES:
        return None
    try:
        return path.read_bytes()
    except OSError as exc:
        logger.warning(f"Could not read recovered recording: {exc}")
        return None
