"""Crash-safe recording recovery: audio kept on disk until it reaches the recordings archive.

While the microphone is recording, IsolatedAudioRecorder spills the raw PCM
stream to an in-progress file. Once the capture is safe in the recordings
archive the spill is discarded; if the app hangs or crashes before that, or
the archive cannot be written, the spill is kept as an unsaved recording and
offered again at the next launch.

Files (raw 16-bit mono 16kHz PCM, no header):
- recording-in-progress.pcm   written live during a recording
- unsaved-recording-<n>.pcm   one per capture that did not reach the archive,
                              numbered in the order they were kept
- last-recording.pcm          the single slot of 0.6.x and earlier, read as
                              the oldest unsaved recording
"""

from __future__ import annotations

import os
from datetime import datetime, timezone
from pathlib import Path

from .audio_format import BYTES_PER_SECOND
from .logger_config import logger

IN_PROGRESS_NAME = "recording-in-progress.pcm"
UNSAVED_PREFIX = "unsaved-recording-"
UNSAVED_SUFFIX = ".pcm"
LEGACY_NAME = "last-recording.pcm"

# Below half a second of audio there is nothing worth recovering.
MIN_RECOVERABLE_BYTES = BYTES_PER_SECOND // 2


def in_progress_path(base_dir: Path) -> Path:
    return base_dir / IN_PROGRESS_NAME


def _size_of(path: Path) -> int:
    """Size in bytes, 0 when missing/unreadable."""
    try:
        return path.stat().st_size
    except OSError:
        return 0


def _place_in_order(name: str) -> int | None:
    """Where a kept file falls in the order unsaved recordings were kept (the
    legacy slot predates them all), or None for any other file."""
    if name == LEGACY_NAME:
        return 0
    number = name.removeprefix(UNSAVED_PREFIX).removesuffix(UNSAVED_SUFFIX)
    if name.startswith(UNSAVED_PREFIX) and name.endswith(UNSAVED_SUFFIX) and number.isdigit():
        return int(number)
    return None


def _kept_files(base_dir: Path) -> list[tuple[int, Path]]:
    """Every unsaved-recording file, whatever its size, oldest first."""
    try:
        paths = list(base_dir.iterdir())
    except FileNotFoundError:
        return []
    placed = ((_place_in_order(path.name), path) for path in paths)
    return sorted((place, path) for place, path in placed if place is not None)


def promote_in_progress(base_dir: Path) -> bool:
    """Keep the in-progress capture as an unsaved recording of its own.

    Returns True when it is kept. Too-short files are deleted rather than
    kept so they don't resurface at next launch.

    An earlier unsaved recording is never replaced or deleted here: each is
    the only copy of its audio. Keeping one is a rename, so it takes no disk
    space the spill was not already using, and the next launch moves every
    one of them into the archive; that is why their number is not capped.
    """
    src = in_progress_path(base_dir)
    size = _size_of(src)
    if size == 0:
        return False
    try:
        if size < MIN_RECOVERABLE_BYTES:
            src.unlink(missing_ok=True)
            return False
        kept = _kept_files(base_dir)
        number = kept[-1][0] + 1 if kept else 1
        src.replace(base_dir / f"{UNSAVED_PREFIX}{number}{UNSAVED_SUFFIX}")
        return True
    except OSError as exc:
        logger.warning(f"Could not preserve recording for recovery: {exc}")
        return False


def keep_unsaved(base_dir: Path, pcm: bytes) -> bool:
    """Keep a capture that has no spill of its own (an earlier capture still
    held the recovery file) as the next unsaved recording. True when it is
    kept; False when it is too short to recover or cannot be written."""
    if len(pcm) < MIN_RECOVERABLE_BYTES:
        return False
    temp = None
    try:
        kept = _kept_files(base_dir)  # Listing the folder can fail too: then nothing is kept.
        path = base_dir / f"{UNSAVED_PREFIX}{kept[-1][0] + 1 if kept else 1}{UNSAVED_SUFFIX}"
        temp = path.with_name(path.name + ".tmp")
        with temp.open("wb") as handle:
            handle.write(pcm)
            handle.flush()
            os.fsync(handle.fileno())
        temp.replace(path)  # A crash leaves no partial recording under the name the next launch reads.
        return True
    except OSError as exc:
        if temp is not None:
            temp.unlink(missing_ok=True)
        logger.error(f"Could not keep a capture of {len(pcm)} bytes as an unsaved recording in {base_dir}: {exc}")
        return False


def unkept_in_progress(base_dir: Path) -> bool:
    """Whether the recovery file still holds a capture worth keeping, which
    promote_in_progress() could not set aside: a new spill must not truncate it."""
    return _size_of(in_progress_path(base_dir)) >= MIN_RECOVERABLE_BYTES


def _discard(path: Path, what: str) -> None:
    try:
        path.unlink(missing_ok=True)
    except OSError as exc:
        logger.warning(f"Could not remove {what}: {exc}")


def discard_in_progress(base_dir: Path) -> None:
    _discard(in_progress_path(base_dir), "in-progress recording")


def unsaved_recordings(base_dir: Path) -> list[Path]:
    """The unsaved recordings worth recovering, oldest first."""
    return [path for _, path in _kept_files(base_dir) if _size_of(path) >= MIN_RECOVERABLE_BYTES]


def captured_at(path: Path) -> datetime:
    """When an unsaved recording's capture ended: the spill's last write,
    which keeping it (a rename) does not change. Raises OSError when the
    file is gone."""
    return datetime.fromtimestamp(path.stat().st_mtime, timezone.utc)


def load_unsaved(path: Path) -> bytes | None:
    """Raw PCM of an unsaved recording, or None when it is gone or too short.

    Reads the whole file into memory — call from a worker thread, not the
    main thread (an hour of audio is ~115 MB).
    """
    if _size_of(path) < MIN_RECOVERABLE_BYTES:
        return None
    try:
        return path.read_bytes()
    except OSError as exc:
        logger.warning(f"Could not read unsaved recording {path.name}: {exc}")
        return None


def discard_unsaved(path: Path) -> None:
    """The recording is safe in the archive now (or unwanted)."""
    _discard(path, f"unsaved recording {path.name}")


def discard_every_unsaved(base_dir: Path) -> None:
    """Every unsaved recording, and what an interrupted keep_unsaved() left."""
    for _, path in _kept_files(base_dir):
        discard_unsaved(path)
    for path in base_dir.glob(f"{UNSAVED_PREFIX}*{UNSAVED_SUFFIX}.tmp"):
        _discard(path, f"interrupted unsaved recording {path.name}")
