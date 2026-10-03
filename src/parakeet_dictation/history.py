"""Transcript history, persisted as JSON."""

from __future__ import annotations

import json
import shutil
import threading
import uuid
from dataclasses import dataclass, field, fields
from datetime import datetime, timezone
from enum import StrEnum
from pathlib import Path

from .atomic_file import remove_leftovers, set_aside, write_text_atomically
from .logger_config import logger


class Source(StrEnum):
    """Where a transcript came from; stored with each history entry."""
    MICROPHONE = "microphone"
    RECOVERY = "recovery"
    FILE = "file"


@dataclass
class HistoryEntry:
    id: str
    created_at: str
    source_kind: str
    source_label: str
    text: str
    raw_text: str = ""
    # Keys written by a newer version, carried through so a save here does
    # not erase them.
    unrecognized: dict = field(default_factory=dict, repr=False)


# What history.json stores per entry. raw_text lives in the sidecar so the
# main file stays readable by 0.3.0, whose loader rejects unknown fields.
_STORED_FIELDS = tuple(f.name for f in fields(HistoryEntry) if f.name not in ("raw_text", "unrecognized"))


def _legacy_history(support_dir: Path) -> Path:
    """Where the app kept transcripts under its earlier name."""
    return support_dir.parent / "ParakeetDictation" / "history.json"


def adopt_legacy_history(support_dir: Path) -> None:
    """Copy the transcript history of the app's earlier name, once."""
    legacy = _legacy_history(support_dir)
    current = support_dir / "history.json"
    if current.exists() or not legacy.exists():
        return
    support_dir.mkdir(parents=True, exist_ok=True)
    shutil.copy2(legacy, current)


class HistoryStore:
    def __init__(self, base_dir: Path, *, history_limit: int):
        self.history_limit = history_limit
        self.base_dir = base_dir
        self.base_dir.mkdir(parents=True, exist_ok=True)
        self.path = self.base_dir / "history.json"
        self.originals_path = self.base_dir / "history-originals.json"
        self._lock = threading.Lock()
        self._entries = self._load()

    def _load(self) -> list[HistoryEntry]:
        if not self.path.exists():
            return []
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
            if not isinstance(payload, list):
                raise ValueError("history is not a list")
        except (ValueError, OSError) as exc:
            # The next save would otherwise overwrite every transcript, and
            # the pre-replacement texts that belong with them.
            set_aside(self.path, str(exc))
            if self.originals_path.exists():
                set_aside(self.originals_path, f"set aside with {self.path.name}: {exc}")
            return []

        originals: dict = {}
        try:
            loaded = json.loads(self.originals_path.read_text(encoding="utf-8"))
            if not isinstance(loaded, dict):
                raise ValueError("history originals are not an object")
            originals = loaded
        except FileNotFoundError:
            pass  # History from before this file existed, or adopted from the earlier name.
        except (ValueError, OSError) as exc:
            # The next save would otherwise overwrite every pre-replacement text.
            set_aside(self.originals_path, str(exc))

        entries = []
        for item in payload:
            if not isinstance(item, dict) or not all(isinstance(item.get(name), str) for name in _STORED_FIELDS):
                continue  # One malformed entry does not hide the rest.
            entry = HistoryEntry(
                **{name: item[name] for name in _STORED_FIELDS},
                unrecognized={key: value for key, value in item.items() if key not in _STORED_FIELDS},
            )
            original = originals.get(entry.id)
            if isinstance(original, str):
                entry.raw_text = original
            entries.append(entry)
        return entries[: self.history_limit]

    def _save(self) -> None:
        entries = self._entries[: self.history_limit]
        payload = [dict(entry.unrecognized) | {name: getattr(entry, name) for name in _STORED_FIELDS}
                   for entry in entries]
        originals = {entry.id: entry.raw_text for entry in entries
                     if entry.raw_text and entry.raw_text != entry.text}
        # Something outside the app may have removed the folder since.
        self.base_dir.mkdir(parents=True, exist_ok=True)
        write_text_atomically(self.originals_path, json.dumps(originals, indent=2))
        write_text_atomically(self.path, json.dumps(payload, indent=2))

    def list_entries(self) -> list[HistoryEntry]:
        # A lower `history_limit` shows at once; the transcripts beyond it go
        # with the next save, so raising it again before then loses nothing.
        with self._lock:
            return self._entries[: self.history_limit]

    def add_entry(self, source_kind: str, source_label: str, text: str, raw_text: str = "") -> HistoryEntry:
        entry = HistoryEntry(
            id=uuid.uuid4().hex,
            created_at=datetime.now(timezone.utc).isoformat(),
            source_kind=source_kind,
            source_label=source_label,
            text=text,
            raw_text=raw_text,
        )

        with self._lock:
            self._entries.insert(0, entry)
            self._entries = self._entries[: self.history_limit]
            try:
                self._save()
            except Exception as exc:
                # The transcript is still published and copied; only its
                # place in history is at risk, and the user is not blocked.
                logger.error(f"Failed to save history: {exc}")

        return entry

    def clear(self) -> bool:
        """Erase every transcript on disk: this history, the copies of it set
        aside as unreadable, and the history of the app's earlier name."""
        with self._lock:
            self._entries = []
            try:
                self._save()
                for path in (self.path, self.originals_path):
                    remove_leftovers(path)
                _legacy_history(self.base_dir).unlink(missing_ok=True)
                return True
            except Exception as exc:
                logger.error(f"Failed to clear history on disk: {exc}")
                return False

    def render(self) -> str | None:
        """History as display text, or None when there is nothing yet."""
        entries = self.list_entries()
        if not entries:
            return None

        blocks = []
        for entry in entries:
            try:
                created_at = datetime.fromisoformat(entry.created_at).astimezone().strftime("%Y-%m-%d %H:%M")
            except ValueError:
                created_at = entry.created_at
            blocks.append(f"[{created_at}] {entry.source_kind.title()}: {entry.source_label}\n{entry.text.strip()}")
            if entry.raw_text and entry.raw_text != entry.text:
                blocks[-1] += f"\n\nBefore word replacements:\n{entry.raw_text.strip()}"

        return "\n\n".join(blocks)
