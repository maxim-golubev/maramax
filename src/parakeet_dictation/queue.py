from __future__ import annotations

import copy
import enum
import os
import threading
import uuid
from dataclasses import dataclass, field


class OutputMode(enum.Enum):
    CLIPBOARD = "clipboard"
    INDIVIDUAL_SAME_DIR = "individual_same_dir"
    INDIVIDUAL_CHOSEN_DIR = "individual_chosen_dir"
    SINGLE_FILE = "single_file"


@dataclass
class OutputConfig:
    mode: OutputMode
    output_path: str | None = None


@dataclass
class QueueItem:
    id: str
    path: str
    filename: str
    status: str = "pending"  # pending | processing | done | failed | cancelled
    result_text: str = ""
    error: str = ""


class TranscriptionQueue:
    def __init__(self):
        self._lock = threading.Lock()
        self._items: list[QueueItem] = []

    def add(self, path: str) -> QueueItem:
        item = QueueItem(
            id=uuid.uuid4().hex,
            path=path,
            filename=os.path.basename(path),
        )
        with self._lock:
            self._items.append(item)
        return item

    def add_many(self, paths: list[str]) -> list[QueueItem]:
        items = [
            QueueItem(id=uuid.uuid4().hex, path=p, filename=os.path.basename(p))
            for p in paths
        ]
        with self._lock:
            self._items.extend(items)
        return items

    def remove(self, item_id: str) -> None:
        with self._lock:
            self._items = [i for i in self._items if i.id != item_id]

    def move(self, item_id: str, new_index: int) -> None:
        with self._lock:
            idx = next((i for i, it in enumerate(self._items) if it.id == item_id), None)
            if idx is None:
                return
            item = self._items.pop(idx)
            new_index = max(0, min(new_index, len(self._items)))
            self._items.insert(new_index, item)

    def clear(self) -> None:
        with self._lock:
            self._items.clear()

    def clear_done(self) -> None:
        with self._lock:
            self._items = [i for i in self._items if i.status not in ("done", "failed", "cancelled")]

    def items(self) -> list[QueueItem]:
        with self._lock:
            return [copy.copy(i) for i in self._items]

    def pending_count(self) -> int:
        with self._lock:
            return sum(1 for i in self._items if i.status == "pending")

    def set_status(self, item_id: str, status: str, result_text: str = "", error: str = "") -> None:
        with self._lock:
            for item in self._items:
                if item.id == item_id:
                    item.status = status
                    if result_text:
                        item.result_text = result_text
                    if error:
                        item.error = error
                    break
