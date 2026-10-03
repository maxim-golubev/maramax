"""Bounded local archive of dictation audio: retryable WAV files with atomic metadata."""

from __future__ import annotations

import json
import math
import os
import threading
import uuid
import wave
from dataclasses import dataclass, field, fields, replace
from datetime import datetime, timezone
from enum import StrEnum
from pathlib import Path

from .atomic_file import write_text_atomically
from .audio_format import CHANNELS, SAMPLE_RATE, SAMPLE_WIDTH, seconds
from .logger_config import logger

MAX_RECORDINGS = 20
MAX_ARCHIVE_BYTES = 512 * 1024 * 1024
# What a recording leaves on disk: its audio, its metadata, and the temporary
# files of an atomic write of either.
_AUDIO_SUFFIX, _METADATA_SUFFIX = ".wav", ".json"
_TEMP_SUFFIXES = (_AUDIO_SUFFIX + ".tmp", _METADATA_SUFFIX + ".tmp")


class RecordingStatus(StrEnum):
    SAVED = "saved"          # archived; recognition has not produced an outcome
    DONE = "done"            # transcribed
    FAILED = "failed"        # recognition ran and produced no transcript
    CANCELLED = "cancelled"  # the user cancelled recognition


@dataclass
class Recording:
    id: str
    created_at: str
    duration: float
    status: str = RecordingStatus.SAVED
    text: str = ""
    message: str = ""
    diagnostics: dict = field(default_factory=dict)
    raw_text: str = ""
    # Metadata keys written by a newer version. They are carried through
    # untouched so that opening a recording here does not erase them.
    unrecognized: dict = field(default_factory=dict, repr=False)


_KNOWN_FIELDS = tuple(f.name for f in fields(Recording) if f.name != "unrecognized")


def recovery_candidate(records: list[Recording]) -> Recording | None:
    """Which recording "Recover Last Recording" should transcribe, given the
    archive newest-first: audio that never reached the recognizer (a crash or
    a quit mid-dictation) before captures that were already tried."""
    untried = [record for record in records if record.status == RecordingStatus.SAVED]
    unfinished = untried or [record for record in records if record.status != RecordingStatus.DONE]
    return unfinished[0] if unfinished else None


class RecordingStore:
    # Always keep the newest recording, even if it alone exceeds the budget.
    def __init__(self, base_dir: Path, limit: int = MAX_RECORDINGS, max_bytes: int = MAX_ARCHIVE_BYTES):
        self.base_dir = base_dir
        self.base_dir.mkdir(parents=True, exist_ok=True)
        self.limit = limit
        self.max_bytes = max_bytes
        self._lock = threading.RLock()

    def audio_path(self, recording_id: str) -> Path:
        if len(recording_id) != 32 or any(c not in "0123456789abcdef" for c in recording_id):
            raise ValueError("Invalid recording identifier")
        return self.base_dir / f"{recording_id}{_AUDIO_SUFFIX}"

    def _metadata_path(self, recording_id: str) -> Path:
        return self.audio_path(recording_id).with_suffix(_METADATA_SUFFIX)

    def _write_metadata(self, record: Recording) -> None:
        payload = dict(record.unrecognized) | {name: getattr(record, name) for name in _KNOWN_FIELDS}
        write_text_atomically(self._metadata_path(record.id), json.dumps(payload, indent=2))

    @staticmethod
    def _from_metadata(payload: object, recording_id: str) -> Recording:
        if not isinstance(payload, dict):
            raise ValueError("Recording metadata is not an object")
        record = Recording(
            **{name: payload[name] for name in _KNOWN_FIELDS if name in payload},
            unrecognized={key: value for key, value in payload.items() if key not in _KNOWN_FIELDS},
        )
        if record.id != recording_id:
            raise ValueError("Mismatched recording identifier")
        if (not isinstance(record.created_at, str)
                or not isinstance(record.duration, (int, float))
                or not math.isfinite(record.duration) or record.duration < 0
                or not isinstance(record.text, str) or not isinstance(record.raw_text, str)
                or not isinstance(record.message, str)
                or not isinstance(record.status, str) or not isinstance(record.diagnostics, dict)):
            raise ValueError("Invalid recording metadata")
        return record

    def list_recordings(self) -> list[Recording]:
        """Newest first. WAV and JSON files appear by atomic rename, so a
        snapshot is read without the writer lock and the UI stays responsive
        during a large save."""
        records = []
        for path in self.base_dir.glob("*.wav"):
            try:
                self.audio_path(path.stem)
                payload = json.loads(path.with_suffix(".json").read_text(encoding="utf-8"))
                record = self._from_metadata(payload, path.stem)
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
                    continue  # Not a readable recording; clear() still removes it.
            records.append(record)
        return sorted(records, key=lambda r: (r.created_at, r.id), reverse=True)

    def save(self, pcm: bytes, diagnostics: dict | None = None) -> Recording | None:
        if not pcm:
            return None
        if len(pcm) % SAMPLE_WIDTH:
            raise ValueError("Expected complete 16-bit PCM samples")
        record = Recording(
            uuid.uuid4().hex, datetime.now(timezone.utc).isoformat(), seconds(pcm),
            diagnostics=dict(diagnostics or {}),
        )
        with self._lock:
            # Something outside the app may have removed the folder since.
            self.base_dir.mkdir(parents=True, exist_ok=True)
            path = self.audio_path(record.id)
            temp = path.with_suffix(_TEMP_SUFFIXES[0])
            try:
                with temp.open("wb") as handle:
                    with wave.open(handle, "wb") as audio:
                        audio.setnchannels(CHANNELS)
                        audio.setsampwidth(SAMPLE_WIDTH)
                        audio.setframerate(SAMPLE_RATE)
                        audio.writeframes(pcm)
                    handle.flush()
                    os.fsync(handle.fileno())
                temp.replace(path)
            finally:
                temp.unlink(missing_ok=True)
            # The recording exists from here: list_recordings rebuilds an
            # entry without metadata. Raising now would make the caller keep
            # the recovery spill too, and the next launch would archive the
            # same audio a second time; so these failures are only logged.
            try:
                self._write_metadata(record)
            except OSError as exc:
                logger.error(f"Recording {record.id} was saved without its details: {exc}")
            # Pruning is after durable audio+metadata, never before saving.
            try:
                self._prune(record.id)
            except OSError as exc:
                logger.error(f"Recording {record.id} was saved, but older recordings could not be pruned: {exc}")
        return record

    def update(self, recording_id: str, **changes) -> None:
        with self._lock:
            record = next((r for r in self.list_recordings() if r.id == recording_id), None)
            if record is None:
                raise FileNotFoundError(f"Recording {recording_id} is no longer in the archive")
            self._write_metadata(replace(record, **changes))

    def load_pcm(self, recording_id: str) -> bytes:
        with self._lock, wave.open(str(self.audio_path(recording_id)), "rb") as audio:
            found = (audio.getnchannels(), audio.getsampwidth(), audio.getframerate())
            if found != (CHANNELS, SAMPLE_WIDTH, SAMPLE_RATE):
                raise ValueError(f"Recording {recording_id} has format {found}, expected "
                                 f"{(CHANNELS, SAMPLE_WIDTH, SAMPLE_RATE)}")
            return audio.readframes(audio.getnframes())

    def _own_files(self, suffixes: tuple[str, ...]) -> list[Path]:
        """Files in the folder named as a recording id plus one of `suffixes`,
        readable or not; anything else there is not ours."""
        found = []
        for path in self.base_dir.iterdir():
            recording_id = path.name.split(".", 1)[0]
            try:
                self.audio_path(recording_id)
            except ValueError:
                continue
            if path.name.removeprefix(recording_id) in suffixes:
                found.append(path)
        return found

    def clear(self) -> None:
        with self._lock:
            if not self.base_dir.exists():
                return  # Removed from outside: nothing is left to clear.
            # Include damaged files and interrupted atomic writes: clearing
            # history must not leave private audio behind just because its
            # header or metadata cannot be parsed.
            for path in self._own_files((_AUDIO_SUFFIX, _METADATA_SUFFIX, *_TEMP_SUFFIXES)):
                path.unlink(missing_ok=True)

    def _prune(self, newest_id: str) -> None:
        # Every write here holds the lock, so a temporary file seen now was
        # left by a write that a crash or a kill interrupted; recovery.py
        # still holds that audio.
        for path in self._own_files(_TEMP_SUFFIXES):
            path.unlink(missing_ok=True)
        total = 0
        records = self.list_recordings()
        records.sort(key=lambda r: r.id != newest_id)
        for index, record in enumerate(records):
            path = self.audio_path(record.id)
            total += path.stat().st_size
            if record.id != newest_id and (index >= self.limit or total > self.max_bytes):
                path.unlink(missing_ok=True)
                self._metadata_path(record.id).unlink(missing_ok=True)
