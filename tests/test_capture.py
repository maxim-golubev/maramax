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


def test_a_slow_driver_open_is_not_counted_as_a_missing_microphone():
    now = [0.0]
    meter = CaptureMeter(clock=lambda: now[0])
    now[0] = 6.5  # A Bluetooth route that took 6.5 s to open.
    meter.mark_open()
    snapshot = meter.snapshot()
    assert snapshot.open_delay == 6.5
    assert snapshot.health == "waiting"
    now[0] = 12
    assert meter.snapshot().health == "missing"


def test_switching_inputs_pauses_the_disconnect_deadline():
    now = [0.0]
    meter = CaptureMeter(clock=lambda: now[0])
    meter.feed(b"\x10\x00" * 512)
    now[0] = 2.5
    meter.set_reconnecting(True)
    now[0] = 6
    assert meter.snapshot().health == "reconnecting"
    meter.set_reconnecting(False)  # The replacement device gets a fresh wait.
    assert meter.snapshot().health == "receiving"
    now[0] = 9.5
    assert meter.snapshot().health == "disconnected"


def test_meter_moves_for_quiet_and_loud_microphones():
    quiet, loud = CaptureMeter(), CaptureMeter()
    quiet.feed(np.array([300, -300] * 256, dtype="<i2").tobytes())   # about -41 dBFS
    loud.feed(np.array([12000, -12000] * 256, dtype="<i2").tobytes())  # about -9 dBFS
    assert 0.2 < quiet.snapshot().level < 0.5
    assert loud.snapshot().level == 1.0
    assert not quiet.snapshot().faint


def test_noise_floor_of_a_muted_microphone_is_faint_not_speech():
    meter = CaptureMeter()
    meter.feed(np.array([15, -12] * 256, dtype="<i2").tobytes())
    assert meter.snapshot().faint
    assert meter.snapshot().health == "receiving"
