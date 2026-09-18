"""Audio health measurements. No device or model is opened by this module."""

from __future__ import annotations

import math
import threading
import time
from dataclasses import asdict, dataclass

import numpy as np


@dataclass(frozen=True)
class CaptureSnapshot:
    elapsed: float
    audio_seconds: float
    first_frame_delay: float | None
    last_frame_age: float | None
    last_signal_age: float | None
    peak: float
    level: float
    nonzero_samples: int
    callbacks: int
    overflow_count: int
    device_name: str

    @property
    def health(self) -> str:
        # Digital silence is evidence of a broken route, not proof that the
        # speaker is quiet. Once signal has arrived, ordinary pauses are fine.
        if self.last_frame_age is None:
            return "waiting" if self.elapsed < 5 else "missing"
        if self.last_frame_age > 3:
            return "disconnected"
        if self.nonzero_samples == 0:
            return "waiting" if self.elapsed < 5 else "silent"
        if self.last_signal_age is not None and self.last_signal_age > 10:
            return "quiet"
        return "receiving"

    def diagnostics(self) -> dict:
        return asdict(self) | {"health": self.health}


class CaptureMeter:
    """Per-recording measurements; stale callbacks retain their old meter."""

    def __init__(self, rate: int = 16000, clock=time.monotonic):
        self._clock = clock
        self._started = clock()
        self._rate = rate
        self._lock = threading.Lock()
        self._samples = 0
        self._nonzero = 0
        self._callbacks = 0
        self._overflows = 0
        self._first: float | None = None
        self._last: float | None = None
        self._last_signal: float | None = None
        self._peak = 0.0
        self._level = 0.0
        self.device_name = "System default"

    def feed(self, pcm: bytes, overflow: bool = False) -> None:
        # An empty callback isn't evidence that an audio route is working.
        if not pcm:
            return
        samples = np.frombuffer(pcm, dtype="<i2").astype(np.float32)
        peak = float(np.max(np.abs(samples))) / 32768.0
        rms = math.sqrt(float(np.mean(samples * samples))) / 32768.0
        now = self._clock()
        with self._lock:
            self._samples += len(samples)
            self._nonzero += int(np.count_nonzero(samples))
            self._callbacks += 1
            self._overflows += int(overflow)
            if self._first is None:
                self._first = now
            self._last = now
            if peak > 0:
                self._last_signal = now
            self._peak = max(self._peak, peak)
            self._level = min(1.0, rms * 12)

    def snapshot(self) -> CaptureSnapshot:
        now = self._clock()
        with self._lock:
            age = None if self._last is None else now - self._last
            return CaptureSnapshot(
                elapsed=now - self._started,
                audio_seconds=self._samples / self._rate,
                first_frame_delay=None if self._first is None else self._first - self._started,
                last_frame_age=age,
                last_signal_age=None if self._last_signal is None else now - self._last_signal,
                peak=self._peak,
                level=self._level if age is not None and age < 0.3 else 0.0,
                nonzero_samples=self._nonzero,
                callbacks=self._callbacks,
                overflow_count=self._overflows,
                device_name=self.device_name,
            )
