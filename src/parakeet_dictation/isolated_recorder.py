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
        # Seconds the helper keeps the device open after a recording so the
        # next one starts instantly. 0 closes it immediately.
        self.keep_warm_seconds = 0
        self._selected_device_name: str | None = None
        self._command = command or worker_command()
        self._start_timeout = start_timeout
        self._stop_timeout = stop_timeout
        self._operation_lock = threading.Lock()
        self._data_lock = threading.Lock()
        self._cancel_start = threading.Event()
        self._ready = threading.Event()
        self._done = threading.Event()
        # Set while the helper is waiting for a request (idle, or holding a
        # warm stream); clear while it records or winds a device down.
        self._accepting = threading.Event()
        self._process: subprocess.Popen[str] | None = None
        self._reader: threading.Thread | None = None
        self._spill: IO[bytes] | None = None
        self._closed = False
        self._overflows = 0
        self._started = False
        self._answered = False
        self._in_flight = False
        self.recording = False
        self.warm_start = False
        self.last_error: Exception | None = None
        self.reset_count = 0
        self.frames: list[bytes] = []
        self.meter = CaptureMeter()
        # What Automatic resolved to at the last device enumeration.
        self.automatic_device_name: str | None = None

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

    # -- Helper process --

    def _popen(self):
        return subprocess.Popen(self._command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                stderr=subprocess.DEVNULL, text=True, bufsize=1)

    def _spawn(self):
        """Callers hold _operation_lock."""
        process = self._popen()
        self._process = process
        self._accepting.set()
        self._reader = threading.Thread(target=self._read, args=(process,), daemon=True)
        self._reader.start()
        return process

    @staticmethod
    def _write(process, text):
        assert process.stdin is not None
        process.stdin.write(text + "\n")
        process.stdin.flush()

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

    def _retire_process(self):
        """Stop the helper and its reader. Callers hold _operation_lock."""
        process, reader = self._process, self._reader
        self._process = None
        self._reader = None
        self._accepting.clear()
        if process is None:
            return
        if process.poll() is None:
            self.reset_count += 1
        self._kill(process)
        if reader is not None:
            reader.join(timeout=2)
            if reader.is_alive():
                # A reader stuck in a disk write still owns the spill handle.
                self._closed = True
                raise RuntimeError("Audio reader could not stop safely")
        self._close_pipes(process)

    def _helper(self):
        """A helper ready for a request, reusing the standby one when it is."""
        process = self._process
        if process is not None:
            # A helper still closing the previous device gets a moment; a
            # wedged one is replaced instead of delaying this recording.
            deadline = time.monotonic() + 1.0
            while process.poll() is None and time.monotonic() < deadline:
                if self._accepting.wait(timeout=0.02):
                    return process
            self._retire_process()
        return self._spawn()

    def prepare(self):
        """Launch the helper ahead of the next recording so starting pays only
        for the driver open. No audio device is touched."""
        if not self._operation_lock.acquire(blocking=False):
            return
        try:
            if self._closed or self.recording:
                return
            process = self._process
            if process is None or process.poll() is not None:
                if process is not None:
                    self._retire_process()
                self._spawn()
        except (OSError, RuntimeError):
            pass  # start() reports a helper that cannot be launched.
        finally:
            self._operation_lock.release()

    def release_device(self):
        """Close a microphone that is being kept ready between dictations."""
        if not self._operation_lock.acquire(timeout=2):
            return
        try:
            process = self._process
            if process is not None and process.poll() is None and not self._in_flight:
                try:
                    self._write(process, json.dumps({"operation": "release"}))
                except (OSError, ValueError):
                    pass
        finally:
            self._operation_lock.release()

    def list_input_devices(self):
        if self.recording or not self._operation_lock.acquire(blocking=False):
            return None
        process = None
        try:
            if self.recording or self._closed:
                return None
            # A separate one-shot helper: a wedged enumeration must not
            # poison the standby process the next recording will use.
            process = self._popen()
            self._write(process, json.dumps({"operation": "list", "prefer_builtin": self.prefer_builtin}))
            result: list[dict] = []
            answered = threading.Event()

            def read():
                assert process.stdout is not None
                try:
                    for line in process.stdout:
                        message = json.loads(line)
                        if message.get("event") in ("devices", "error"):
                            result.append(message)
                            answered.set()
                            return
                except (ValueError, OSError):
                    pass
                finally:
                    answered.set()

            reader = threading.Thread(target=read, daemon=True)
            reader.start()
            answered.wait(timeout=4)
            for message in result:
                if message.get("event") == "devices":
                    self.automatic_device_name = message.get("automatic")
                    return [InputDevice(*item) for item in message["devices"]]
            return None
        except OSError:
            return None
        finally:
            if process is not None:
                self._kill(process)
                self._close_pipes(process)
            self._operation_lock.release()

    # -- Recording --

    def start(self):
        deadline = time.monotonic() + self._start_timeout
        if not self._operation_lock.acquire(timeout=min(4, self._start_timeout)):
            self.last_error = TimeoutError("Microphone is busy — try again")
            return False
        try:
            if self._closed:
                self.last_error = RuntimeError("Maramax is shutting down")
                return False
            if self.recording:
                self.last_error = RuntimeError("A recording is already in progress")
                return False
            self.preserve_recovery(only_if_larger=True)
            request = json.dumps({"operation": "record", "device": self._selected_device_name,
                                  "prefer_builtin": self.prefer_builtin,
                                  "keep_warm": self.keep_warm_seconds})
            for attempt in range(2):
                # Taking the helper first also reaps a finished one, so its
                # reader cannot signal into the state that is reset below.
                process = self._helper()
                self.last_error = None
                with self._data_lock:
                    self.frames = []
                    self.meter = CaptureMeter()
                    self._overflows = 0
                self._ready.clear()
                self._done.clear()
                self._started = False
                self._answered = False
                self.warm_start = False
                self._accepting.clear()
                self._in_flight = True
                self._open_spill()
                try:
                    self._write(process, request)
                except (OSError, ValueError):
                    pass  # Noticed below as a helper that exited without answering.
                while not self._ready.wait(timeout=0.02):
                    if self._cancel_start.is_set() or self._closed or time.monotonic() >= deadline:
                        break
                    if process.poll() is not None:
                        # Exited; its reader may still be delivering an answer.
                        self._ready.wait(timeout=0.2)
                        break
                silent_exit = not self._answered and process.poll() is not None
                if self._cancel_start.is_set():
                    self.last_error = RuntimeError("Microphone connection cancelled")
                elif silent_exit and attempt == 0 and not self._closed:
                    # The standby helper died before answering (it never
                    # reached the device); a fresh one takes over.
                    self._in_flight = False
                    self._retire_process()
                    self._close_spill()
                    continue
                elif silent_exit:
                    self.last_error = RuntimeError("The audio helper stopped unexpectedly — try again")
                elif not self._ready.is_set():
                    self.last_error = TimeoutError("Microphone connection timed out and was reset — try again")
                elif self._started and self.last_error is None:
                    self.recording = True
                    return True
                break
            self._abandon_start()
            return False
        except Exception as exc:
            self.last_error = exc
            try:
                self._abandon_start()
            except RuntimeError:
                pass
            return False
        finally:
            self._cancel_start.clear()
            self._operation_lock.release()

    def _open_spill(self):
        try:
            self._recovery_dir.mkdir(parents=True, exist_ok=True)
            self._spill = recovery.in_progress_path(self._recovery_dir).open("wb")
        except OSError:
            self._spill = None  # Memory capture and final archive can still succeed.

    def _abandon_start(self):
        self._in_flight = False
        # A helper that answered with an error has already let go of the
        # device and stays useful; one that never answered, or whose start
        # was cancelled mid-open, may be wedged in a driver call.
        if not self._answered or self._started:
            self._retire_process()
        self._close_spill()

    def cancel_start(self):
        self._cancel_start.set()

    def _read(self, process):
        try:
            assert process.stdout is not None
            for line in process.stdout:
                message = json.loads(line)
                if process is not self._process:
                    return  # Replaced helper: its late output belongs to no recording.
                event = message.get("event")
                if event == "audio":
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
                elif event == "ready":
                    self.meter.device_name = message["device"]
                    self.meter.mark_open()
                    self.warm_start = bool(message.get("warm"))
                    self._started = True
                    self._answered = True
                    self._ready.set()
                elif event == "reconnecting":
                    self.meter.set_reconnecting(True)
                elif event == "device":
                    self.meter.device_name = message.get("device") or self.meter.device_name
                    self.meter.set_reconnecting(False)
                elif event == "error":
                    self.last_error = RuntimeError(message.get("message", "Microphone failed"))
                    self._answered = True
                    self._ready.set()
                    self._done.set()
                elif event == "done":
                    self._done.set()
                elif event in ("idle", "warm"):
                    self._accepting.set()
        except Exception as exc:
            if process is self._process and self._in_flight:
                self.last_error = exc
        finally:
            if process is self._process:
                if self._in_flight and not self._done.is_set() and self.last_error is None:
                    self.last_error = RuntimeError("Microphone connection ended unexpectedly")
                self._ready.set()
                self._done.set()

    def stop(self):
        with self._operation_lock:
            process = self._process
            if process is not None and self._in_flight:
                try:
                    self._write(process, "stop")
                except (OSError, ValueError):
                    if self.last_error is None:
                        self.last_error = RuntimeError("Microphone stopped responding; received audio was retained")
                # "done" follows the last audio on the same pipe, so the
                # buffer is complete once it arrives. The helper closes the
                # device afterwards, on its own time.
                if not self._done.wait(timeout=self._stop_timeout):
                    self.last_error = RuntimeError("Microphone stopped responding; received audio was retained")
                    try:
                        self._retire_process()
                    except RuntimeError as exc:
                        self.last_error = exc
            self._in_flight = False
            self.recording = False
            # A cancel aimed at this recording must not abort the next one.
            self._cancel_start.clear()
            self._close_spill()
            locked = self._data_lock.acquire(timeout=1.0)
            try:
                pcm = b"".join(self.frames)
                self.frames = []
            finally:
                if locked:
                    self._data_lock.release()
            return pcm

    def _close_spill(self):
        locked = self._data_lock.acquire(timeout=1.0)
        try:
            if self._spill is not None:
                self._spill.close()
                self._spill = None
        finally:
            if locked:
                self._data_lock.release()

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
        with self._operation_lock:
            process = self._process
            if process is not None and process.stdin is not None:
                try:
                    process.stdin.close()  # EOF: the helper releases the device and exits.
                    process.wait(timeout=0.5)
                except (OSError, ValueError, subprocess.TimeoutExpired):
                    pass
            resets = self.reset_count
            try:
                self._retire_process()
            except RuntimeError:
                pass
            self.reset_count = resets  # Shutting down a healthy helper is not a reset.
