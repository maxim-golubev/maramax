"""Transcript history persisted as JSON; replaced originals live in a sidecar file."""

from __future__ import annotations

import json
import shutil
import threading
import uuid
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path

from .logger_config import setup_logging

logger = setup_logging()


@dataclass
class HistoryEntry:
    id: str
    created_at: str
    source_kind: str
    source_label: str
    text: str
    raw_text: str = ""


class HistoryStore:
    def __init__(self, history_limit: int = 100, base_dir: Path | None = None):
        self.history_limit = history_limit
        self.base_dir = base_dir or self._default_base_dir()
        self.base_dir.mkdir(parents=True, exist_ok=True)
        self.path = self.base_dir / "history.json"
        self.originals_path = self.base_dir / "history-originals.json"
        self._lock = threading.Lock()
        self._entries = self._load()

    @staticmethod
    def _default_base_dir() -> Path:
        support_dir = Path.home() / "Library" / "Application Support"
        maramax_dir = support_dir / "Maramax"
        legacy_dir = support_dir / "ParakeetDictation"
        legacy_history = legacy_dir / "history.json"
        maramax_history = maramax_dir / "history.json"

        if maramax_history.exists() or not legacy_history.exists():
            return maramax_dir

        maramax_dir.mkdir(parents=True, exist_ok=True)
        if not maramax_history.exists():
            shutil.copy2(legacy_history, maramax_history)
        return maramax_dir

    def _load(self) -> list[HistoryEntry]:
        if not self.path.exists():
            return []

        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError, UnicodeDecodeError):
            return []

        entries = []
        try:
            originals = json.loads(self.originals_path.read_text(encoding="utf-8"))
            if not isinstance(originals, dict):
                originals = {}
        except (OSError, ValueError, UnicodeDecodeError):
            originals = {}
        for item in payload if isinstance(payload, list) else []:
            if not isinstance(item, dict):
                continue

            try:
                entry = HistoryEntry(**item)
                if not all(isinstance(value, str) for value in asdict(entry).values()):
                    continue
                original = originals.get(entry.id)
                if isinstance(original, str):
                    entry.raw_text = original
                entries.append(entry)
            except TypeError:
                continue

        return entries[: self.history_limit]

    def _save(self) -> None:
        entries = self._entries[: self.history_limit]
        # Keep history.json readable by 0.3.0, whose loader rejects unknown
        # fields. Originals live in a bounded sidecar keyed by entry ID.
        payload = []
        originals = {}
        for entry in entries:
            item = asdict(entry)
            item.pop("raw_text")
            payload.append(item)
            if entry.raw_text and entry.raw_text != entry.text:
                originals[entry.id] = entry.raw_text
        original_temp = self.originals_path.with_suffix(".json.tmp")
        original_temp.write_text(json.dumps(originals, indent=2), encoding="utf-8")
        original_temp.replace(self.originals_path)
        temp_path = self.path.with_suffix(f"{self.path.suffix}.tmp")
        temp_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        temp_path.replace(self.path)

    def list_entries(self) -> list[HistoryEntry]:
        with self._lock:
            return list(self._entries)

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
                logger.error(f"Failed to save history: {exc}")

        return entry

    def clear(self) -> bool:
        with self._lock:
            self._entries = []
            try:
                self._save()
                return True
            except Exception as exc:
                logger.error(f"Failed to save history: {exc}")
                return False

    def render(self) -> str:
        entries = self.list_entries()
        if not entries:
            return (
                "No transcriptions yet.\n\n"
                "Use Option+Space to dictate or drop audio/video files into the overlay."
            )

        blocks = []
        for entry in entries:
            try:
                created_at = datetime.fromisoformat(entry.created_at).astimezone().strftime("%Y-%m-%d %H:%M")
            except Exception:
                created_at = str(entry.created_at)
            blocks.append(f"[{created_at}] {entry.source_kind.title()}: {entry.source_label}\n{entry.text.strip()}")
            if entry.raw_text and entry.raw_text != entry.text:
                blocks[-1] += f"\n\nBefore word replacements:\n{entry.raw_text.strip()}"

        return "\n\n".join(blocks)
