"""Write completed queue transcripts to the clipboard or to text files."""

from __future__ import annotations

import enum
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import assert_never

from .clipboard import ClipboardError, copy_text
from .file_queue import QueuedFile, QueueStatus


class OutputMode(enum.Enum):
    """The destinations that need no place chosen by the user."""
    CLIPBOARD = "clipboard"
    NEXT_TO_ORIGINALS = "next_to_originals"


@dataclass(frozen=True)
class ToFolder:
    """One text file per transcript, in a folder the user chose."""
    path: Path


@dataclass(frozen=True)
class ToFile:
    """Every transcript in one text file the user chose."""
    path: Path


# Where a queue run's transcripts go. A destination that needs a path carries
# one, and one that does not cannot be given one.
Destination = OutputMode | ToFolder | ToFile


class ExportError(RuntimeError):
    """Why the transcripts did not reach their destination. The message is the
    cause alone, worded to follow "Export failed: "."""


def export_results(items: list[QueuedFile], destination: Destination) -> str:
    """Deliver the completed transcripts among `items`; returns what was done,
    worded for the status line."""
    completed = [i for i in items if i.status == QueueStatus.DONE and i.result_text]
    if not completed:
        raise ExportError("no completed transcripts to export")

    match destination:
        case OutputMode.CLIPBOARD:
            _copy_to_clipboard(completed)
            return f"Copied {_transcripts(len(completed))} to clipboard"
        case OutputMode.NEXT_TO_ORIGINALS:
            _save_each(completed, lambda source: source.parent)
            originals = "its original" if len(completed) == 1 else "their originals"
            return f"Saved {_transcripts(len(completed))} next to {originals}"
        case ToFolder(path=folder):
            _save_each(completed, lambda _source: folder)
            return f"Saved {_transcripts(len(completed))} to {folder.name}"
        case ToFile(path=path):
            _save_together(completed, path)
            return f"Saved {_transcripts(len(completed))} to {path.name}"
        case _:
            assert_never(destination)


def _transcripts(count: int) -> str:
    return f"{count} transcript{'s' if count != 1 else ''}"


def _reason(exc: OSError) -> str:
    """What went wrong, without the errno and path that str() adds: the
    message names the file itself."""
    return exc.strerror if exc.strerror is not None else str(exc)


def _joined(items: list[QueuedFile]) -> str:
    if len(items) == 1:
        return items[0].result_text
    return "\n\n".join(f"## {i.filename}\n\n{i.result_text}" for i in items)


def _copy_to_clipboard(items: list[QueuedFile]) -> None:
    try:
        copy_text(_joined(items))
    except ClipboardError as exc:
        raise ExportError("could not copy to the clipboard") from exc


def _save_each(items: list[QueuedFile], folder_for: Callable[[Path], Path]) -> None:
    """One text file per transcript, named after its source, in the folder
    `folder_for` gives for that source. An existing file is never replaced.
    One that cannot be written does not keep the rest from being saved: the
    error names the first and says how many were."""
    failures: list[tuple[str, OSError]] = []
    for item in items:
        source = Path(item.path)
        folder = folder_for(source)
        try:
            folder.mkdir(parents=True, exist_ok=True)
            out_path = folder / f"{source.stem}.txt"
            counter = 1
            while out_path.exists():
                counter += 1
                out_path = folder / f"{source.stem}_{counter}.txt"
            out_path.write_text(item.result_text, encoding="utf-8")
        except OSError as exc:
            failures.append((f"could not write {source.stem}.txt to {folder.name}: {_reason(exc)}", exc))
    if failures:
        message, cause = failures[0]
        saved = len(items) - len(failures)
        if saved:
            message += f" ({saved} of {_transcripts(len(items))} saved; the rest are in History)"
        raise ExportError(message) from cause


def _save_together(items: list[QueuedFile], path: Path) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(_joined(items), encoding="utf-8")
    except OSError as exc:
        raise ExportError(f"could not write {path.name}: {_reason(exc)}") from exc
