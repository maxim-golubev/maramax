"""Thread-safe list of media files waiting for batch transcription."""

from __future__ import annotations

import copy
import enum
import os
import threading
import uuid
from dataclasses import dataclass


class QueueStatus(enum.StrEnum):
    PENDING = "pending"
    PROCESSING = "processing"
    DONE = "done"
    FAILED = "failed"
    CANCELLED = "cancelled"


@dataclass
class QueuedFile:
    id: str
    path: str
    filename: str
    status: QueueStatus = QueueStatus.PENDING
    result_text: str = ""
    error: str = ""


class TranscriptionQueue:
    def __init__(self):
        self._lock = threading.Lock()
        self._files: list[QueuedFile] = []

    def add_many(self, paths: list[str]) -> list[QueuedFile]:
        files = [QueuedFile(id=uuid.uuid4().hex, path=p, filename=os.path.basename(p)) for p in paths]
        with self._lock:
            self._files.extend(files)
        return files

    def remove(self, file_id: str) -> None:
        with self._lock:
            self._files = [f for f in self._files if f.id != file_id]

    def move(self, file_id: str, new_index: int) -> None:
        with self._lock:
            index = next((i for i, f in enumerate(self._files) if f.id == file_id), None)
            if index is None:
                return
            moved = self._files.pop(index)
            self._files.insert(max(0, min(new_index, len(self._files))), moved)

    def clear(self) -> None:
        with self._lock:
            self._files.clear()

    def requeue_cancelled(self) -> None:
        """A cancelled run can be started again; files that failed stay failed."""
        with self._lock:
            for queued in self._files:
                if queued.status == QueueStatus.CANCELLED:
                    queued.status = QueueStatus.PENDING

    def items(self) -> list[QueuedFile]:
        with self._lock:
            return [copy.copy(f) for f in self._files]

    def pending_count(self) -> int:
        with self._lock:
            return sum(1 for f in self._files if f.status == QueueStatus.PENDING)

    def set_status(self, file_id: str, status: QueueStatus, result_text: str = "", error: str = "") -> None:
        with self._lock:
            for queued in self._files:
                if queued.id == file_id:
                    queued.status = status
                    if result_text:
                        queued.result_text = result_text
                    if error:
                        queued.error = error
                    break
