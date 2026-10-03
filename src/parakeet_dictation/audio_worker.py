"""The audio helper process: it owns the microphone on the app's behalf.

No GUI, speech model, or application state. The app starts it ahead of time
so a recording pays only for the driver open, and replaces it whenever it
stops answering. The messages are defined in helper_protocol.py.
"""

from __future__ import annotations

import base64
import os
import queue
import sys
import threading
import time

from .capture import CaptureSnapshot
from .helper_protocol import Event, Operation, event, parse
from .logger_config import setup_helper_logging
from .recorder import AudioRecorder, default_input_device, lid_closed

# Speech that is still in the driver (or in a Bluetooth link) when the user
# presses stop would otherwise lose its last syllable.
TAIL_SECONDS = 0.2
# A live input delivers a buffer every ~32 ms. This long without one means the
# device is gone or its route collapsed; shorter gaps are left alone because
# reopening a Bluetooth input costs seconds of its own.
STALL_SECONDS = 1.5
# A stream that has delivered nothing at all may still be connecting (slow
# Bluetooth opens of ~3 s have been observed), so it gets longer.
NO_AUDIO_SECONDS = 4.0
# Bluetooth headsets send zeros while switching into headset mode (1.5-2.5 s
# is normal). Far beyond that the route is broken and worth rebuilding once.
SILENT_ROUTE_SECONDS = 6.0
MAX_REOPENS = 2
POLL_SECONDS = 0.02

_EOF = object()


def send(kind: Event, **fields) -> None:
    print(event(kind, **fields), flush=True)


def route_failed(snapshot: CaptureSnapshot, listening: float,
                 callbacks_at_open: int, nonzero_at_open: int) -> bool:
    """Whether the open input has failed and a replacement should be tried.

    `listening` is the time since this stream was opened; the two counters
    are what the meter read at that moment, so a replacement stream is
    judged on what it delivers rather than on its predecessor's audio.
    """
    if snapshot.callbacks == callbacks_at_open:
        return listening > NO_AUDIO_SECONDS
    if snapshot.last_frame_age is not None and snapshot.last_frame_age > STALL_SECONDS:
        return True
    return snapshot.nonzero_samples == nonzero_at_open and listening > SILENT_ROUTE_SECONDS


class RequestReader:
    """Lines from the app. EOF ends the helper even if a native call hangs."""

    def __init__(self) -> None:
        self._queue: queue.Queue = queue.Queue()
        threading.Thread(target=self._read, daemon=True).start()

    def _read(self) -> None:
        for line in sys.stdin:
            self._queue.put(line.strip())
        self._queue.put(_EOF)
        # The app exited or crashed. Give the main loop a moment to answer a
        # one-shot request and close the device; a native open/close may be
        # wedged, so don't rely on Python cleanup to release an orphan.
        time.sleep(5)
        os._exit(0)

    def get(self, timeout: float | None = None):
        try:
            return self._queue.get(timeout=timeout)
        except queue.Empty:
            return None


class AudioHelper:
    def __init__(self) -> None:
        self.requests = RequestReader()
        self.recorder: AudioRecorder | None = None
        # Set while a finished recording's stream is deliberately kept open.
        self._warm_key: tuple | None = None
        self._warm_until = 0.0

    def run(self) -> None:
        try:
            while True:
                line = self.requests.get(0.25 if self._warm_key is not None else None)
                if line is _EOF:
                    return
                if line is None:
                    self._tend_warm_stream()
                    continue
                request = parse(line)
                if request is None:
                    continue
                operation = request.get("operation")
                if operation == Operation.PING:
                    from .isolated_recorder import worker_command
                    send(Event.PONG, command=worker_command())
                elif operation == Operation.LIST:
                    self._list(request)
                elif operation == Operation.RELEASE:
                    if self._warm_key is not None:
                        self._close_warm_stream()
                elif operation == Operation.RECORD:
                    if self._record(request) is _EOF:
                        return
                # A STOP that arrives after its recording ended needs no answer.
        finally:
            self._release()
            self._exit()

    @staticmethod
    def _exit() -> None:
        sys.stdout.flush()
        os._exit(0)

    def _release(self) -> None:
        self._warm_key = None
        recorder, self.recorder = self.recorder, None
        if recorder is not None and not recorder.cleanup():
            # A wedged close leaked its PortAudio session, so PortAudio stays
            # initialized here with its device list frozen: every later
            # recording in this process would look for devices in that stale
            # list. The app starts a fresh helper when this one is gone.
            self._exit()

    def _close_warm_stream(self) -> None:
        """Close the kept-warm device, telling the app first (CLOSING): a
        close can take a while, or wedge."""
        send(Event.CLOSING)
        self._release()
        send(Event.IDLE)

    @staticmethod
    def _stream_is_delivering(recorder: AudioRecorder) -> bool:
        age = recorder.capture_snapshot().last_frame_age
        return recorder.stream_active() and age is not None and age < STALL_SECONDS

    def _tend_warm_stream(self) -> None:
        recorder = self.recorder
        if recorder is None or self._warm_key is None:
            return
        del recorder.frames[:]  # Audio heard while waiting is never kept.
        if not self._stream_is_delivering(recorder):
            # The device went away while waiting. Closing a dead route is
            # where PortAudio wedges; leaving is quicker and always works.
            self._exit()
        if time.monotonic() >= self._warm_until:
            self._close_warm_stream()

    def _list(self, request: dict) -> None:
        self._release()
        recorder = AudioRecorder(prefer_builtin=bool(request.get("prefer_builtin", True)))
        try:
            names = recorder.list_input_devices()
            if names is None:
                send(Event.ERROR, message="the audio session was busy")
            else:
                send(Event.DEVICES, devices=names, automatic=recorder.automatic_device_name())
        except Exception as exc:
            send(Event.ERROR, message=str(exc))
        finally:
            recorder.cleanup()

    @staticmethod
    def _stream_key(request: dict) -> tuple:
        """What decides which device a request opens. A kept-warm stream is
        reused only for an identical key; the lid matters because closing it
        switches the built-in microphone off, and Automatic follows a system
        default input chosen since the stream opened (AirPods connecting)."""
        device = request.get("device")
        prefer_builtin = bool(request.get("prefer_builtin", True))
        if device is not None:
            return device, prefer_builtin, None, None
        return device, prefer_builtin, (lid_closed() if prefer_builtin else None), default_input_device()

    def _record(self, request: dict):
        key = self._stream_key(request)
        recorder = self.recorder
        warm = False
        if recorder is not None:
            if not self._stream_is_delivering(recorder):
                # Leave rather than close a dead route (see _tend_warm_stream);
                # the app retries a request whose helper exits unanswered.
                self._exit()
            warm = self._warm_key == key and recorder.rearm()
            if not warm:
                self._release()
        self._warm_key = None
        if not warm:
            recorder = self.recorder = AudioRecorder(device_name=key[0], prefer_builtin=key[1])
            if not recorder.start():
                send(Event.ERROR, message=str(recorder.last_error))
                self._release()
                send(Event.IDLE)
                return None
        assert recorder is not None
        send(Event.READY, device=recorder.capture_snapshot().device_name, warm=bool(warm))

        reopens = 0
        opened = time.monotonic()
        callbacks_at_open = nonzero_at_open = 0
        keep_warm = 0.0
        try:
            while True:
                line = self.requests.get(POLL_SECONDS)
                if line is _EOF:
                    return _EOF
                stop = parse(line) if line is not None else None
                if stop is not None and stop.get("operation") == Operation.STOP:
                    seconds = stop.get("keep_warm", 0)
                    keep_warm = float(seconds) if isinstance(seconds, (int, float)) else 0.0
                    break
                self._forward(recorder)
                snapshot = recorder.capture_snapshot()
                if reopens < MAX_REOPENS and route_failed(
                        snapshot, time.monotonic() - opened, callbacks_at_open, nonzero_at_open):
                    send(Event.RECONNECTING)
                    reopened = recorder.reopen()
                    snapshot = recorder.capture_snapshot()
                    if reopened:
                        send(Event.DEVICE, device=snapshot.device_name, reopened=True)
                    else:
                        send(Event.DEVICE, device=snapshot.device_name, reopened=False,
                             error=str(recorder.last_error))
                    # One honest failure is enough: the app ends the recording
                    # with what was captured instead of showing a dead one.
                    reopens = reopens + 1 if reopened else MAX_REOPENS
                    opened = time.monotonic()
                    callbacks_at_open, nonzero_at_open = snapshot.callbacks, snapshot.nonzero_samples
            deadline = time.monotonic() + TAIL_SECONDS
            while time.monotonic() < deadline:
                time.sleep(POLL_SECONDS)
                self._forward(recorder)
            self._forward(recorder)
            healthy = recorder.last_error is None and self._stream_is_delivering(recorder)
            # Everything captured is delivered before the device is closed:
            # the app can start recognition while the driver winds down.
            send(Event.DONE)
            if keep_warm > 0 and healthy:
                self._warm_key = key
                self._warm_until = time.monotonic() + keep_warm
                send(Event.WARM)
            else:
                self._release()
                send(Event.IDLE)
        except Exception as exc:
            send(Event.ERROR, message=str(exc))
            self._release()
            send(Event.IDLE)
        return None

    @staticmethod
    def _forward(recorder: AudioRecorder) -> None:
        """Send what has been captured since the last call and drop it here:
        the app holds the recording, so the helper keeps no second copy. The
        stream callback only appends, so removing the sent head is safe."""
        frames = recorder.frames
        count = len(frames)
        if not count:
            return
        send(Event.AUDIO, pcm=base64.b64encode(b"".join(frames[:count])).decode("ascii"),
             overflows=recorder.capture_snapshot().overflow_count)
        del frames[:count]


def main() -> None:
    setup_helper_logging()
    AudioHelper().run()
