from __future__ import annotations

import os
from pathlib import Path

from .clipboard import ClipboardError, copy_text
from .queue import OutputConfig, OutputMode, QueueItem


class ExportError(RuntimeError):
    pass


def export_results(items: list[QueueItem], config: OutputConfig) -> str:
    completed = [i for i in items if i.status == "done" and i.result_text]
    if not completed:
        raise ExportError("No completed transcriptions to export")

    if config.mode == OutputMode.CLIPBOARD:
        return _export_clipboard(completed)
    elif config.mode == OutputMode.INDIVIDUAL_SAME_DIR:
        return _export_individual(completed, target_dir=None)
    elif config.mode == OutputMode.INDIVIDUAL_CHOSEN_DIR:
        if not config.output_path:
            raise ExportError("No output directory specified")
        return _export_individual(completed, target_dir=config.output_path)
    elif config.mode == OutputMode.SINGLE_FILE:
        if not config.output_path:
            raise ExportError("No output file specified")
        return _export_single_file(completed, config.output_path)
    else:
        raise ExportError(f"Unknown output mode: {config.mode}")


def _export_clipboard(items: list[QueueItem]) -> str:
    if len(items) == 1:
        text = items[0].result_text
    else:
        sections = [f"## {i.filename}\n\n{i.result_text}" for i in items]
        text = "\n\n".join(sections)

    try:
        copy_text(text)
    except ClipboardError as exc:
        raise ExportError(f"Clipboard copy failed: {exc}") from exc

    count = len(items)
    return f"Copied {count} transcript{'s' if count != 1 else ''} to clipboard"


def _export_individual(items: list[QueueItem], target_dir: str | None) -> str:
    written = 0

    for item in items:
        source = Path(item.path)
        stem = source.stem
        out_dir = Path(target_dir) if target_dir else source.parent
        out_dir.mkdir(parents=True, exist_ok=True)

        out_path = out_dir / f"{stem}.txt"
        counter = 1
        while out_path.exists():
            counter += 1
            out_path = out_dir / f"{stem}_{counter}.txt"

        try:
            out_path.write_text(item.result_text, encoding="utf-8")
            written += 1
        except OSError as exc:
            raise ExportError(f"Failed to write {out_path.name}: {exc}") from exc

    dir_label = target_dir or "source directories"
    return f"Saved {written} file{'s' if written != 1 else ''} to {dir_label}"


def _export_single_file(items: list[QueueItem], output_path: str) -> str:
    if len(items) == 1:
        text = items[0].result_text
    else:
        sections = [f"## {i.filename}\n\n{i.result_text}" for i in items]
        text = "\n\n".join(sections)

    path = Path(output_path)
    path.parent.mkdir(parents=True, exist_ok=True)

    try:
        path.write_text(text, encoding="utf-8")
    except OSError as exc:
        raise ExportError(f"Failed to write {path.name}: {exc}") from exc

    return f"Saved transcript to {path.name}"
