"""Bounded microphone operations; native driver calls live in a disposable process."""

from __future__ import annotations

import base64
import json
import os
import subprocess
import sys
import threading
import time
from dataclasses import replace
from pathlib import Path
from typing import IO

from . import recovery
from .capture import CaptureMeter
from .paths import app_support_dir
from .recorder import InputDevice


def worker_command() -> list[str]:
    if getattr(sys, "frozen", False):
        resources = os.environ.get("RESOURCEPATH")
        executable = Path(resources).parent / "MacOS" / "Maramax" if resources else Path(sys.executable)
        return [str(executable), "--audio-worker"]
    return [sys.executable, "-m", "parakeet_dictation.main", "--audio-worker"]


class IsolatedAudioRecorder:
    rate = 16000
    channels = 1
    abandoned_sessions = 0

    def __init__(self, recovery_dir: Path | None = None, prefer_builtin=True,
                 command: list[str] | None = None, start_timeout=8.0, stop_timeout=3.0):
        self._recovery_dir = recovery_dir or app_support_dir()
        self.prefer_builtin = prefer_builtin
        self._selected_device_name: str | None = None
        self._command = command or worker_command()
        self._start_timeout = start_timeout
        self._stop_timeout = stop_timeout
        self._operation_lock = threading.Lock()
        self._data_lock = threading.Lock()
        self._cancel_start = threading.Event()
        self._ready = threading.Event()
        self._done = threading.Event()
        self._process: subprocess.Popen[str] | None = None
        self._reader: threading.Thread | None = None
        self._spill: IO[bytes] | None = None
        self._closed = False
        self._overflows = 0
        self._started = False
        self.recording = False
        self.last_error: Exception | None = None
        self.reset_count = 0
        self.frames: list[bytes] = []
        self.meter = CaptureMeter()

    @property
    def recovery_dir(self):
        return self._recovery_dir

    def sample_width(self):
        return 2

    def set_device(self, name):
        self._selected_device_name = name

    def get_selected_device_name(self):
        return self._selected_device_name

    def capture_snapshot(self):
        return replace(self.meter.snapshot(), overflow_count=self._overflows)

    def is_recording(self):
        return self.recording

    def _spawn(self, operation):
        process = subprocess.Popen(self._command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                   stderr=subprocess.DEVNULL, text=True, bufsize=1)
        assert process.stdin is not None
        try:
            process.stdin.write(json.dumps({"operation": operation, "device": self._selected_device_name,
                                            "prefer_builtin": self.prefer_builtin}) + "\n")
            process.stdin.flush()
        except OSError:
            self._kill(process)
            self._close_pipes(process)
            raise
        return process

    @staticmethod
    def _close_pipes(process):
        for pipe in (process.stdin, process.stdout):
            if pipe is not None:
                try:
                    pipe.close()
                except OSError:
                    pass  # A dead child can leave buffered input unwritable.

    @staticmethod
    def _kill(process):
        if process.poll() is None:
            process.kill()
        process.wait(timeout=2)

    def list_input_devices(self):
        if self.recording or not self._operation_lock.acquire(blocking=False):
            return None
        process = None
        try:
            if self.recording or self._closed:
                return None
            process = self._spawn("list")
            # Keep stdin open: EOF means the parent disappeared.
            result = []
            def read():
                assert process.stdout is not None
                for line in process.stdout:
                    result.append(json.loads(line))
            reader = threading.Thread(target=read, daemon=True)
            reader.start()
            try:
                process.wait(timeout=4)
            except subprocess.TimeoutExpired:
                self._kill(process)
            reader.join(timeout=1)
            for message in result:
                if message.get("event") == "devices":
                    return [InputDevice(*item) for item in message["devices"]]
            return None
        finally:
            if process is not None:
                if process.poll() is None:
                    self._kill(process)
                self._close_pipes(process)
            self._operation_lock.release()

    def start(self):
        deadline = time.monotonic() + self._start_timeout
        if not self._operation_lock.acquire(timeout=min(4, self._start_timeout)):
            self.last_error = TimeoutError("Microphone is busy — try again")
            return False
        try:
            if self._closed or self.recording:
                return False
            self.last_error = None
            self.frames = []
            self.meter = CaptureMeter()
            self._overflows = 0
            self._ready.clear()
            self._done.clear()
            self._started = False
            self.preserve_recovery(only_if_larger=True)
            try:
                self._recovery_dir.mkdir(parents=True, exist_ok=True)
                self._spill = recovery.in_progress_path(self._recovery_dir).open("wb")
            except OSError:
                self._spill = None  # Memory capture and final archive can still succeed.
            self._process = self._spawn("record")
            self._reader = threading.Thread(target=self._read, args=(self._process,), daemon=True)
            self._reader.start()
            while not self._ready.wait(timeout=0.02):
                if self._cancel_start.is_set() or time.monotonic() >= deadline:
                    break
            if self._cancel_start.is_set():
                self.last_error = RuntimeError("Microphone connection cancelled")
            elif not self._ready.is_set():
                self.last_error = TimeoutError("Microphone connection timed out and was reset — try again")
            elif self._started and self.last_error is None:
                self.recording = True
                return True
            self._reset_process()
            self._close_spill()
            return False
        except Exception as exc:
            self.last_error = exc
            self._reset_process()
            self._close_spill()
            return False
        finally:
            self._cancel_start.clear()
            self._operation_lock.release()

    def cancel_start(self):
        self._cancel_start.set()

    def _read(self, process):
        try:
            assert process.stdout is not None
            for line in process.stdout:
                message = json.loads(line)
                event = message.get("event")
                if event == "ready":
                    self.meter.device_name = message["device"]
                    self._started = True
                    self._ready.set()
                elif event == "audio":
                    pcm = base64.b64decode(message["pcm"], validate=True)
                    if len(pcm) % 2:
                        raise ValueError("Incomplete audio sample")
                    with self._data_lock:
                        self.frames.append(pcm)
                        self.meter.feed(pcm)
                        self._overflows = message.get("overflows", self._overflows)
                        if self._spill is not None:
                            try:
                                self._spill.write(pcm)
                                self._spill.flush()
                            except OSError:
                                self._spill.close()
                                self._spill = None
                elif event == "error":
                    self.last_error = RuntimeError(message.get("message", "Microphone failed"))
                    self._ready.set()
                elif event == "done":
                    self._done.set()
        except Exception as exc:
            self.last_error = exc
        finally:
            if not self._done.is_set() and self.last_error is None:
                self.last_error = RuntimeError("Microphone connection ended unexpectedly")
            self._ready.set()

    def _reset_process(self):
        process = self._process
        if process is not None:
            if process.poll() is None:
                self.reset_count += 1
                self._kill(process)
            if self._reader is not None:
                self._reader.join(timeout=2)
            if self._reader is not None and self._reader.is_alive():
                self._closed = True
                raise RuntimeError("Audio reader could not stop safely")
            self._close_pipes(process)
        self._process = None
        self._reader = None

    def stop(self):
        with self._operation_lock:
            process = self._process
            if process is not None:
                try:
                    assert process.stdin is not None
                    process.stdin.write("stop\n")
                    process.stdin.flush()
                    process.wait(timeout=self._stop_timeout)
                except (BrokenPipeError, OSError, subprocess.TimeoutExpired):
                    self.last_error = RuntimeError("Microphone stopped responding; received audio was retained")
                finally:
                    self._reset_process()
            self.recording = False
            self._close_spill()
            with self._data_lock:
                pcm = b"".join(self.frames)
                self.frames = []
            return pcm

    def _close_spill(self):
        with self._data_lock:
            if self._spill is not None:
                self._spill.close()
                self._spill = None

    def preserve_recovery(self, only_if_larger=False):
        self._close_spill()
        return recovery.promote_in_progress(self._recovery_dir, only_if_larger)

    def discard_recovery(self):
        self._close_spill()
        recovery.discard_in_progress(self._recovery_dir)

    def has_recoverable_recording(self):
        return recovery.has_last_recording(self._recovery_dir)

    def load_recoverable_recording(self):
        return recovery.load_last_recording(self._recovery_dir)

    def discard_recoverable_recording(self):
        recovery.discard_last_recording(self._recovery_dir)

    def cleanup(self):
        self._cancel_start.set()
        if self._closed:
            return
        self._closed = True
        self.stop()
