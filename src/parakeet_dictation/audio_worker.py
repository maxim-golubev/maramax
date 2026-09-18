"""Disposable audio process. No GUI, speech model, or application state."""

from __future__ import annotations

import base64
import json
import os
import sys
import threading
import time
from pathlib import Path

from .recorder import AudioRecorder


def send(message: dict) -> None:
    print(json.dumps(message), flush=True)


def main() -> None:
    request = json.loads(sys.stdin.readline())
    if request.get("operation") == "ping":
        from .isolated_recorder import worker_command
        send({"event": "pong", "command": worker_command()})
        return
    stop = threading.Event()

    def watch_parent():
        for line in sys.stdin:
            if line.strip() == "stop":
                stop.set()
        # The app exited/crashed. A native open/close may be wedged, so don't
        # rely on Python cleanup to release the orphaned audio process.
        os._exit(0)

    threading.Thread(target=watch_parent, daemon=True).start()
    recorder = AudioRecorder(recovery_dir=Path("/dev/null"), prefer_builtin=request.get("prefer_builtin", True))
    # The main app owns durable recovery. Killing this helper must not leave
    # duplicate temporary recordings behind.
    recorder._open_recovery_file = lambda: None  # type: ignore[method-assign]
    try:
        if request.get("operation") == "list":
            devices = recorder.list_input_devices()
            send({"event": "devices", "devices": [list(device) for device in devices or []]})
            return
        recorder.set_device(request.get("device"))
        if not recorder.start():
            send({"event": "error", "message": str(recorder.last_error)})
            return
        send({"event": "ready", "device": recorder.capture_snapshot().device_name})
        sent_frames = 0
        sent_bytes = 0
        while not stop.is_set():
            frames = recorder.frames[sent_frames:]
            if frames:
                pcm = b"".join(frames)
                send({"event": "audio", "pcm": base64.b64encode(pcm).decode("ascii"),
                      "overflows": recorder.capture_snapshot().overflow_count})
                sent_frames += len(frames)
                sent_bytes += len(pcm)
            time.sleep(0.02)
        pcm = recorder.stop()
        if len(pcm) > sent_bytes:
            send({"event": "audio", "pcm": base64.b64encode(pcm[sent_bytes:]).decode("ascii")})
        send({"event": "done"})
    except Exception as exc:
        send({"event": "error", "message": str(exc)})
    finally:
        recorder.cleanup()
