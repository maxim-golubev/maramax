"""Health measurements of a recording's audio stream. No device or model is opened here."""

from __future__ import annotations

import math
import threading
import time
from dataclasses import asdict, dataclass
from enum import StrEnum

import numpy as np

from .audio_format import FULL_SCALE, SAMPLE_RATE

# Below this peak (about -54 dBFS) a capture holds no usable speech: it is the
# noise floor of a muted or gated microphone, not a quiet talker.
FAINT_PEAK = 0.002

# How long the app waits before it gives up on a microphone. The audio helper
# tries to repair a route sooner than each of these (see audio_worker.py);
# tests/test_capture.py holds the two sets of numbers in that order.
NO_FRAME_SECONDS = 5.0    # device open, but no buffer has ever arrived
NO_SIGNAL_SECONDS = 7.0   # buffers arrive, but none has held a sample above zero since the open
STALLED_SECONDS = 3.0     # buffers were arriving and stopped
QUIET_SECONDS = 10.0      # signal had arrived, then nothing above zero for this long
RECONNECT_LIMIT_SECONDS = 8.0  # a replacement input is taking this long to open


class CaptureHealth(StrEnum):
    WAITING = "waiting"
    RECEIVING = "receiving"
    RECONNECTING = "reconnecting"
    SILENT = "silent"
    QUIET = "quiet"
    MISSING = "missing"
    DISCONNECTED = "disconnected"


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
    # Seconds from the capture request until the device reported itself open
    # (for a replacement input, until it was reopened).
    open_delay: float | None = None
    # Seconds since the helper began opening a replacement input, if it is.
    reconnecting_seconds: float | None = None

    @property
    def health(self) -> CaptureHealth:
        if self.reconnecting_seconds is not None and self.reconnecting_seconds < RECONNECT_LIMIT_SECONDS:
            return CaptureHealth.RECONNECTING
        # A slow driver open (Bluetooth can take seconds) is not time spent
        # listening; the frame deadlines start once the stream exists.
        listening = self.elapsed - (self.open_delay or 0.0)
        # Digital silence is evidence of a broken route, not proof that the
        # speaker is quiet. Once signal has arrived, ordinary pauses are fine.
        if self.last_frame_age is None:
            return CaptureHealth.WAITING if listening < NO_FRAME_SECONDS else CaptureHealth.MISSING
        if self.last_frame_age > STALLED_SECONDS:
            return CaptureHealth.DISCONNECTED
        if self.nonzero_samples == 0:
            return CaptureHealth.WAITING if listening < NO_SIGNAL_SECONDS else CaptureHealth.SILENT
        if self.last_signal_age is not None and self.last_signal_age > QUIET_SECONDS:
            return CaptureHealth.QUIET
        return CaptureHealth.RECEIVING

    def summary(self) -> str:
        """The microphone and how long it has recorded, as the bar and the window show it."""
        seconds = int(self.audio_seconds)
        return f"{self.device_name} · {seconds // 60}:{seconds % 60:02d}"

    @property
    def faint(self) -> bool:
        return self.peak < FAINT_PEAK

    def diagnostics(self) -> dict:
        return asdict(self) | {"health": str(self.health)}


def _display_level(rms: float) -> float:
    """Map RMS to 0..1 on a decibel scale so quiet built-in microphones and
    loud headsets both move the meter (-55 dBFS is empty, -10 dBFS is full)."""
    if rms <= 0:
        return 0.0
    return min(1.0, max(0.0, (20 * math.log10(rms) + 55) / 45))


class CaptureMeter:
    """Per-recording measurements; stale callbacks retain their old meter."""

    def __init__(self, rate: int = SAMPLE_RATE, clock=time.monotonic):
        self._clock = clock
        self._rate = rate
        self._lock = threading.Lock()
        self.device_name = "System default"
        self._reset(opened=False)

    def _reset(self, opened: bool) -> None:
        now = self._clock()
        self._started = now
        self._opened: float | None = now if opened else None
        self._samples = 0
        self._nonzero = 0
        self._callbacks = 0
        self._overflows = 0
        self._first: float | None = None
        self._last: float | None = None
        self._last_signal: float | None = None
        self._reconnecting_since: float | None = None
        self._peak = 0.0
        self._level = 0.0

    def restart(self) -> None:
        """Begin measuring a new recording on a device that is already open."""
        with self._lock:
            self._reset(opened=True)

    def mark_open(self) -> None:
        """The device stream exists; frame deadlines count from here."""
        with self._lock:
            if self._opened is None:
                self._opened = self._clock()

    def set_reconnecting(self, reconnecting: bool) -> None:
        """A replacement stream is being opened mid-recording. Clearing the
        flag treats the new device as just opened: until its first buffer
        it gets the no-buffer deadline, which outlasts the helper's own, so
        a slow replacement is repaired again rather than given up on."""
        with self._lock:
            now = self._clock()
            if reconnecting:
                self._reconnecting_since = now
                return
            self._reconnecting_since = None
            self._opened = now
            self._last = None

    def feed(self, pcm: bytes, overflow: bool = False) -> None:
        # An empty callback isn't evidence that an audio route is working.
        if not pcm:
            return
        samples = np.frombuffer(pcm, dtype="<i2").astype(np.float32)
        peak = float(np.max(np.abs(samples))) / FULL_SCALE
        rms = math.sqrt(float(np.mean(samples * samples))) / FULL_SCALE
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
            self._level = _display_level(rms)

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
                open_delay=None if self._opened is None else self._opened - self._started,
                reconnecting_seconds=(None if self._reconnecting_since is None
                                      else now - self._reconnecting_since),
            )
