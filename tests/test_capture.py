import numpy as np

from parakeet_dictation.capture import CaptureMeter


def test_silent_frames_never_become_a_healthy_microphone():
    now = [0.0]
    meter = CaptureMeter(clock=lambda: now[0])
    for second in range(7):
        now[0] = float(second)
        meter.feed(bytes(32000))
    snapshot = meter.snapshot()
    assert snapshot.health == "silent"
    assert snapshot.audio_seconds == 7
    assert snapshot.nonzero_samples == 0
    assert snapshot.level == 0


def test_empty_callbacks_do_not_vouch_for_a_working_route():
    now = [0.0]
    meter = CaptureMeter(clock=lambda: now[0])
    meter.feed(b"")
    now[0] = 6
    assert meter.snapshot().health == "missing"
    assert meter.snapshot().callbacks == 0


def test_pauses_are_not_mistaken_for_disconnection():
    now = [0.0]
    meter = CaptureMeter(clock=lambda: now[0])
    meter.feed(np.array([100, -100] * 256, dtype="<i2").tobytes())
    now[0] = 12
    meter.feed(bytes(1024))
    assert meter.snapshot().health == "quiet"
    now[0] = 16
    assert meter.snapshot().health == "disconnected"
    assert meter.snapshot().level == 0


def test_signal_returning_after_silence_clears_the_notice():
    now = [0.0]
    meter = CaptureMeter(clock=lambda: now[0])
    meter.feed(b"\x01\x00" * 512)
    now[0] = 12
    meter.feed(bytes(1024))
    assert meter.snapshot().health == "quiet"
    meter.feed(b"\x01\x00" * 512)
    assert meter.snapshot().health == "receiving"


def test_peak_and_overflow_measurements_are_per_recording():
    meter = CaptureMeter()
    meter.feed(np.array([-32768, 32767], dtype="<i2").tobytes(), overflow=True)
    snapshot = meter.snapshot()
    assert snapshot.peak == 1
    assert snapshot.overflow_count == 1
    assert snapshot.audio_seconds == 2 / 16000
    assert CaptureMeter().snapshot().overflow_count == 0
