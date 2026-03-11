from __future__ import annotations

import json
import logging
import shutil
import threading
import uuid
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger("parakeet_dictation")


@dataclass
class HistoryEntry:
    id: str
    created_at: str
    source_kind: str
    source_label: str
    text: str


class HistoryStore:
    def __init__(self, history_limit: int = 100, base_dir: Path | None = None):
        self.history_limit = history_limit
        self.base_dir = base_dir or self._default_base_dir()
        self.base_dir.mkdir(parents=True, exist_ok=True)
        self.path = self.base_dir / "history.json"
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
        except (json.JSONDecodeError, OSError):
            return []

        entries = []
        for item in payload if isinstance(payload, list) else []:
            if not isinstance(item, dict):
                continue

            try:
                entries.append(HistoryEntry(**item))
            except TypeError:
                continue

        return entries[: self.history_limit]

    def _save(self) -> None:
        payload = [asdict(entry) for entry in self._entries[: self.history_limit]]
        temp_path = self.path.with_suffix(f"{self.path.suffix}.tmp")
        temp_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        temp_path.replace(self.path)

    def list_entries(self) -> list[HistoryEntry]:
        with self._lock:
            return list(self._entries)

    def add_entry(self, source_kind: str, source_label: str, text: str) -> HistoryEntry:
        entry = HistoryEntry(
            id=uuid.uuid4().hex,
            created_at=datetime.now(timezone.utc).isoformat(),
            source_kind=source_kind,
            source_label=source_label,
            text=text,
        )

        with self._lock:
            self._entries.insert(0, entry)
            self._entries = self._entries[: self.history_limit]
            try:
                self._save()
            except Exception as exc:
                logger.error(f"Failed to save history: {exc}")

        return entry

    def clear(self) -> None:
        with self._lock:
            self._entries = []
            try:
                self._save()
            except Exception as exc:
                logger.error(f"Failed to save history: {exc}")

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

        return "\n\n".join(blocks)
