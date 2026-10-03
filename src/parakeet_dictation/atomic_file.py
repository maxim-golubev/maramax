"""Crash-safe handling of the app's small JSON files."""

from __future__ import annotations

import glob
import os
from pathlib import Path

from .logger_config import logger


def write_text_atomically(path: Path, text: str) -> None:
    """Replace a file's contents so that a crash or a kill leaves either the
    old version or the new one, never a partial file."""
    temp = path.with_name(path.name + ".tmp")
    try:
        with temp.open("w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            # Hands the data to the drive before the rename. (macOS does not
            # flush the drive's own cache here; a power cut in the same
            # instant can still lose this one write, not the previous file.)
            os.fsync(handle.fileno())
        temp.replace(path)
    except BaseException:
        temp.unlink(missing_ok=True)
        raise


def set_aside(path: Path, why: str) -> None:
    """Move a file that cannot be read out of the way, so the next save does
    not overwrite the only copy of whatever is still recoverable in it. An
    earlier set-aside copy is never replaced."""
    kept = path.with_name(path.name + ".corrupt")
    number = 1
    while kept.exists():
        number += 1
        kept = path.with_name(f"{path.name}.corrupt-{number}")
    try:
        path.rename(kept)
        logger.error(f"{path.name} could not be read ({why}); kept as {kept.name} and starting fresh")
    except OSError as exc:
        logger.error(f"{path.name} could not be read ({why}) and could not be set aside: {exc}")


def remove_leftovers(path: Path) -> None:
    """Delete what this module may have left beside `path`: copies set aside
    by set_aside() and the temporary file of an interrupted write. For a
    caller erasing the file's contents for good."""
    for leftover in (path.with_name(path.name + ".tmp"), path.with_name(path.name + ".corrupt"),
                     *path.parent.glob(f"{glob.escape(path.name)}.corrupt-*")):
        leftover.unlink(missing_ok=True)
