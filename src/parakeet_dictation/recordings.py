"""Bounded local recordings with atomic metadata and retryable WAV audio."""

from __future__ import annotations

import json
import math
import threading
import uuid
import wave
from dataclasses import asdict, dataclass, field, replace
from datetime import datetime, timezone
from pathlib import Path


@dataclass
class Recording:
    id: str
    created_at: str
    duration: float
    status: str = "saved"
    text: str = ""
    message: str = ""
    diagnostics: dict = field(default_factory=dict)
    raw_text: str = ""


class RecordingStore:
    # Always keep the newest recording, even if it alone exceeds the budget.
    def __init__(self, base_dir: Path, limit: int = 20, max_bytes: int = 512 * 1024 * 1024):
        self.base_dir = base_dir
        self.base_dir.mkdir(parents=True, exist_ok=True)
        self.limit = limit
        self.max_bytes = max_bytes
        self._lock = threading.RLock()

    def audio_path(self, recording_id: str) -> Path:
        if len(recording_id) != 32 or any(c not in "0123456789abcdef" for c in recording_id):
            raise ValueError("Invalid recording identifier")
        return self.base_dir / f"{recording_id}.wav"

    def _metadata_path(self, recording_id: str) -> Path:
        return self.audio_path(recording_id).with_suffix(".json")

    def _write_metadata(self, record: Recording) -> None:
        path = self._metadata_path(record.id)
        temp = path.with_suffix(".json.tmp")
        temp.write_text(json.dumps(asdict(record), indent=2), encoding="utf-8")
        temp.replace(path)

    def list_recordings(self) -> list[Recording]:
        # WAV and JSON files appear by atomic rename. Reading a snapshot
        # without the writer lock keeps the UI responsive during a large save.
        records = []
        for path in self.base_dir.glob("*.wav"):
            try:
                payload = json.loads(path.with_suffix(".json").read_text(encoding="utf-8"))
                record = Recording(**payload)
                if record.id != path.stem:
                    raise ValueError("Mismatched recording identifier")
                self.audio_path(record.id)
                if (not isinstance(record.created_at, str)
                        or not isinstance(record.duration, (int, float))
                        or not math.isfinite(record.duration) or record.duration < 0
                        or not isinstance(record.text, str) or not isinstance(record.raw_text, str)
                        or not isinstance(record.message, str)
                        or not isinstance(record.status, str) or not isinstance(record.diagnostics, dict)):
                    raise ValueError("Invalid recording metadata")
            except (OSError, ValueError, TypeError):
                # A crash between WAV and metadata writes must not hide
                # the audio. Reconstruct a minimal, retryable entry.
                try:
                    self.audio_path(path.stem)
                    with wave.open(str(path), "rb") as audio:
                        duration = audio.getnframes() / audio.getframerate()
                    created = datetime.fromtimestamp(path.stat().st_mtime, timezone.utc).isoformat()
                    record = Recording(path.stem, created, duration, message="Recovered saved audio")
                except (OSError, ValueError, wave.Error, EOFError):
                    continue
            records.append(record)
        return sorted(records, key=lambda r: (r.created_at, r.id), reverse=True)

    def save(self, pcm: bytes, diagnostics: dict | None = None) -> Recording | None:
        if not pcm:
            return None
        if len(pcm) % 2:
            raise ValueError("Expected complete 16-bit PCM samples")
        record = Recording(
            uuid.uuid4().hex, datetime.now(timezone.utc).isoformat(), len(pcm) / 32000,
            diagnostics=dict(diagnostics or {}),
        )
        with self._lock:
            path = self.audio_path(record.id)
            temp = path.with_suffix(".wav.tmp")
            try:
                with wave.open(str(temp), "wb") as audio:
                    audio.setnchannels(1)
                    audio.setsampwidth(2)
                    audio.setframerate(16000)
                    audio.writeframes(pcm)
                temp.replace(path)
                self._write_metadata(record)
            finally:
                temp.unlink(missing_ok=True)
            # Pruning is after durable audio+metadata, never before saving.
            self._prune(record.id)
        return record

    def update(self, recording_id: str, **changes) -> None:
        with self._lock:
            record = next((r for r in self.list_recordings() if r.id == recording_id), None)
            if record is None:
                raise FileNotFoundError("Recording no longer available")
            self._write_metadata(replace(record, **changes))

    def load_pcm(self, recording_id: str) -> bytes:
        with self._lock, wave.open(str(self.audio_path(recording_id)), "rb") as audio:
            if (audio.getnchannels(), audio.getsampwidth(), audio.getframerate()) != (1, 2, 16000):
                raise ValueError("Unexpected recording format")
            return audio.readframes(audio.getnframes())

    def clear(self) -> None:
        with self._lock:
            # Include damaged files and interrupted atomic writes: clearing
            # history must not leave private audio behind just because its
            # header or metadata cannot be parsed.
            for path in self.base_dir.iterdir():
                recording_id = path.name.split(".", 1)[0]
                try:
                    self.audio_path(recording_id)
                except ValueError:
                    continue
                if path.name in {recording_id + suffix for suffix in (".wav", ".json", ".wav.tmp", ".json.tmp")}:
                    path.unlink(missing_ok=True)

    def _prune(self, newest_id: str) -> None:
        total = 0
        records = self.list_recordings()
        records.sort(key=lambda r: r.id != newest_id)
        for index, record in enumerate(records):
            path = self.audio_path(record.id)
            total += path.stat().st_size
            if record.id != newest_id and (index >= self.limit or total > self.max_bytes):
                path.unlink(missing_ok=True)
                self._metadata_path(record.id).unlink(missing_ok=True)
