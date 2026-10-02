"""Disposable audio process. No GUI, speech model, or application state.

The app starts this helper ahead of time so a recording only pays for the
driver open, not for a process launch. It reads one JSON request per line on
stdin (`record`, `list`, `ping`), a bare `stop` line ends a recording, and
EOF means the app is gone.
"""

from __future__ import annotations

import base64
import json
import os
import queue
import sys
import threading
import time
from pathlib import Path

from .recorder import AudioRecorder

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

_EOF = object()


def send(message: dict) -> None:
    print(json.dumps(message), flush=True)


class Commands:
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


class Worker:
    def __init__(self) -> None:
        self.commands = Commands()
        self.recorder: AudioRecorder | None = None
        # Set while a finished recording's stream is deliberately kept open.
        self._warm_key: tuple | None = None
        self._warm_until = 0.0

    def run(self) -> None:
        try:
            while True:
                line = self.commands.get(0.25 if self._warm_key is not None else None)
                if line is _EOF:
                    return
                if line is None:
                    self._tend_warm_stream()
                    continue
                try:
                    request = json.loads(line)
                except ValueError:
                    continue  # A late "stop" for a recording that already ended.
                if not isinstance(request, dict):
                    continue
                operation = request.get("operation")
                if operation == "ping":
                    from .isolated_recorder import worker_command
                    send({"event": "pong", "command": worker_command()})
                elif operation == "list":
                    self._list(request)
                elif operation == "release":
                    # The user turned off "keep microphone ready".
                    if self._warm_key is not None:
                        self._release()
                        send({"event": "idle"})
                elif operation == "record":
                    if self._record(request) is _EOF:
                        return
        finally:
            self._release()
            sys.stdout.flush()
            os._exit(0)

    def _new_recorder(self, request: dict) -> AudioRecorder:
        recorder = AudioRecorder(recovery_dir=Path("/dev/null"),
                                 prefer_builtin=bool(request.get("prefer_builtin", True)))
        # The main app owns durable recovery. Killing this helper must not
        # leave duplicate temporary recordings behind.
        recorder._open_recovery_file = lambda: None  # type: ignore[method-assign]
        return recorder

    def _release(self) -> None:
        self._warm_key = None
        recorder, self.recorder = self.recorder, None
        if recorder is not None:
            recorder.cleanup()

    def _tend_warm_stream(self) -> None:
        recorder = self.recorder
        if recorder is None or self._warm_key is None:
            return
        del recorder.frames[:]  # Audio heard while waiting is never kept.
        age = recorder.capture_snapshot().last_frame_age
        if time.monotonic() >= self._warm_until or age is None or age > STALL_SECONDS:
            self._release()
            send({"event": "idle"})

    def _list(self, request: dict) -> None:
        self._release()
        recorder = self._new_recorder(request)
        try:
            devices = recorder.list_input_devices()
            send({"event": "devices", "devices": [list(device) for device in devices or []],
                  "automatic": recorder.automatic_device_name()})
        except Exception as exc:
            send({"event": "error", "message": str(exc)})
        finally:
            recorder.cleanup()

    def _record(self, request: dict):
        key = (request.get("device"), bool(request.get("prefer_builtin", True)))
        recorder = self.recorder
        warm = False
        if recorder is not None and self._warm_key == key and recorder.stream_active():
            age = recorder.capture_snapshot().last_frame_age
            warm = age is not None and age < STALL_SECONDS and recorder.rearm()
        self._warm_key = None
        if not warm:
            self._release()
            recorder = self.recorder = self._new_recorder(request)
            recorder.set_device(key[0])
            if not recorder.start():
                send({"event": "error", "message": str(recorder.last_error)})
                self._release()
                send({"event": "idle"})
                return None
        assert recorder is not None
        overflow_base = recorder.capture_snapshot().overflow_count if warm else 0
        send({"event": "ready", "device": recorder.capture_snapshot().device_name, "warm": bool(warm)})

        sent = 0
        reopens = 0
        opened = time.monotonic()
        ended = None
        try:
            while True:
                line = self.commands.get(0.02)
                if line is _EOF or line == "stop":
                    ended = line
                    break
                sent = self._forward(recorder, sent, overflow_base)
                if reopens < MAX_REOPENS and self._route_failed(recorder, opened):
                    reopens += 1
                    send({"event": "reconnecting"})
                    reopened = recorder.reopen()
                    opened = time.monotonic()
                    send({"event": "device", "device": recorder.capture_snapshot().device_name,
                          "reopened": bool(reopened)})
            if ended is _EOF:
                return _EOF
            deadline = time.monotonic() + TAIL_SECONDS
            while time.monotonic() < deadline:
                time.sleep(0.02)
                sent = self._forward(recorder, sent, overflow_base)
            self._forward(recorder, sent, overflow_base)
            keep_warm = float(request.get("keep_warm") or 0)
            healthy = recorder.last_error is None and recorder.stream_active()
            # Everything captured is delivered before the device is closed:
            # the app can start recognition while the driver winds down.
            send({"event": "done"})
            if keep_warm > 0 and healthy:
                self._warm_key = key
                self._warm_until = time.monotonic() + keep_warm
                send({"event": "warm"})
            else:
                self._release()
                send({"event": "idle"})
        except Exception as exc:
            send({"event": "error", "message": str(exc)})
            self._release()
            send({"event": "idle"})
        return None

    @staticmethod
    def _forward(recorder: AudioRecorder, sent: int, overflow_base: int) -> int:
        frames = recorder.frames[sent:]
        if not frames:
            return sent
        send({"event": "audio", "pcm": base64.b64encode(b"".join(frames)).decode("ascii"),
              "overflows": recorder.capture_snapshot().overflow_count - overflow_base})
        return sent + len(frames)

    @staticmethod
    def _route_failed(recorder: AudioRecorder, opened: float) -> bool:
        snapshot = recorder.capture_snapshot()
        listening = time.monotonic() - opened
        if snapshot.last_frame_age is None:
            return listening > NO_AUDIO_SECONDS
        if snapshot.last_frame_age > STALL_SECONDS and listening > STALL_SECONDS:
            return True
        return snapshot.nonzero_samples == 0 and listening > SILENT_ROUTE_SECONDS


def main() -> None:
    Worker().run()
