"""The one PCM format Maramax captures, stores, and recognizes: 16 kHz, mono, signed 16-bit."""

from __future__ import annotations

SAMPLE_RATE = 16000
SAMPLE_WIDTH = 2  # bytes per sample
CHANNELS = 1
BYTES_PER_SECOND = SAMPLE_RATE * SAMPLE_WIDTH * CHANNELS
# Divides a signed 16-bit sample into the -1.0..1.0 range the recognizers expect.
FULL_SCALE = 32768.0


def seconds(pcm: bytes) -> float:
    return len(pcm) / BYTES_PER_SECOND


def whole_samples(pcm: bytes) -> bytes:
    """Drop a trailing partial sample (an interrupted write can leave one)."""
    return pcm[: len(pcm) - len(pcm) % SAMPLE_WIDTH]
