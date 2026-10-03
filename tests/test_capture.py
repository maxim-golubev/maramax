import numpy as np

from parakeet_dictation.capture import CaptureMeter


def test_silent_frames_never_become_a_healthy_microphone():
    now = [0.0]
    meter = CaptureMeter(clock=lambda: now[0])
    for second in range(8):
        now[0] = float(second)
        meter.feed(bytes(32000))
    snapshot = meter.snapshot()
    assert snapshot.health == "silent"
    assert snapshot.audio_seconds == 8
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
    assert meter.snapshot().health == "waiting"
    now[0] = 6.5
    meter.feed(b"\x10\x00" * 512)
    assert meter.snapshot().health == "receiving"
    now[0] = 10
    assert meter.snapshot().health == "disconnected"


def test_replacement_for_a_stream_that_never_delivered_gets_its_own_wait():
    now = [0.0]
    meter = CaptureMeter(clock=lambda: now[0])
    meter.mark_open()
    now[0] = 4.5  # The helper gives up on the silent stream and reopens.
    meter.set_reconnecting(True)
    now[0] = 5.5
    meter.set_reconnecting(False)
    assert meter.snapshot().health == "waiting"  # Not "missing" before the new stream's first buffer.
    now[0] = 6
    meter.feed(b"\x10\x00" * 512)
    assert meter.snapshot().health == "receiving"


def test_a_reconnect_that_never_finishes_is_a_disconnection():
    now = [0.0]
    meter = CaptureMeter(clock=lambda: now[0])
    meter.feed(b"\x10\x00" * 512)
    now[0] = 2
    meter.set_reconnecting(True)
    now[0] = 9
    assert meter.snapshot().health == "reconnecting"
    now[0] = 10.5
    assert meter.snapshot().health == "disconnected"


def test_restart_measures_a_new_recording_on_an_open_device():
    now = [0.0]
    meter = CaptureMeter(clock=lambda: now[0])
    meter.device_name = "AirPods"
    meter.feed(b"\x10\x00" * 512, overflow=True)
    now[0] = 30
    meter.restart()
    snapshot = meter.snapshot()
    assert (snapshot.nonzero_samples, snapshot.callbacks, snapshot.overflow_count, snapshot.peak) == (0, 0, 0, 0)
    assert snapshot.open_delay == 0 and snapshot.device_name == "AirPods"
    assert snapshot.health == "waiting"


def test_helper_repairs_a_route_before_the_app_gives_up_on_it():
    from parakeet_dictation import audio_worker, capture

    # If the app's deadlines were shorter, it would stop a recording the
    # helper was about to rescue.
    assert audio_worker.STALL_SECONDS < capture.STALLED_SECONDS
    assert audio_worker.NO_AUDIO_SECONDS < capture.NO_FRAME_SECONDS
    assert audio_worker.SILENT_ROUTE_SECONDS < capture.NO_SIGNAL_SECONDS

    # A route that only ever delivers zeros: the helper rebuilds it after
    # SILENT_ROUTE_SECONDS, and until then the app is still waiting rather
    # than telling the user to check their input.
    now = [0.0]
    meter = CaptureMeter(clock=lambda: now[0])
    meter.mark_open()
    while now[0] < audio_worker.SILENT_ROUTE_SECONDS + 0.1:
        meter.feed(bytes(1024))
        now[0] += 0.032
    assert audio_worker.route_failed(meter.snapshot(), now[0], 0, 0)
    assert meter.snapshot().health == "waiting"

    # A replacement for a stream that had delivered: the helper gives it
    # NO_AUDIO_SECONDS for a first buffer before reopening again, so the app
    # must still be waiting then, not reporting a disconnection.
    now = [0.0]
    meter = CaptureMeter(clock=lambda: now[0])
    meter.mark_open()
    meter.feed(b"\x10\x00" * 512)
    now[0] = 1.6
    meter.set_reconnecting(True)
    now[0] = 2.0
    meter.set_reconnecting(False)
    now[0] += audio_worker.NO_AUDIO_SECONDS + 0.1
    assert meter.snapshot().health == "waiting"


def test_route_failure_is_judged_on_what_the_current_stream_delivered():
    from parakeet_dictation.audio_worker import route_failed

    now = [0.0]
    meter = CaptureMeter(clock=lambda: now[0])
    assert not route_failed(meter.snapshot(), 3.9, 0, 0)
    assert route_failed(meter.snapshot(), 4.1, 0, 0)       # Opened, never delivered.
    meter.feed(bytes(1024))
    now[0] = 2.5
    meter.feed(bytes(1024))
    assert not route_failed(meter.snapshot(), 2.5, 0, 0)   # Zeros while a headset connects.
    now[0] = 6.5
    meter.feed(bytes(1024))
    assert route_failed(meter.snapshot(), 6.5, 0, 0)       # Still only zeros: the route is broken.
    meter.feed(b"\x10\x00" * 512)
    assert not route_failed(meter.snapshot(), 6.6, 0, 0)
    now[0] = 8.2
    assert route_failed(meter.snapshot(), 8.2, 0, 0)       # Buffers stopped.
    replaced = meter.snapshot()
    assert not route_failed(replaced, 1.6, replaced.callbacks, replaced.nonzero_samples)  # New stream, fair wait.
    assert route_failed(replaced, 4.1, replaced.callbacks, replaced.nonzero_samples)


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
