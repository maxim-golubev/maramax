"""The app's microphone: bounded start and stop around an audio helper process that it can replace."""

from __future__ import annotations

import base64
import os
import subprocess
import sys
import threading
import time
from dataclasses import replace
from pathlib import Path
from typing import IO

from . import recovery
from .capture import CaptureMeter, CaptureSnapshot
from .helper_protocol import Event, Operation, parse, request
from .logger_config import logger


def worker_command() -> list[str]:
    if getattr(sys, "frozen", False):
        resources = os.environ.get("RESOURCEPATH")
        executable = Path(resources).parent / "MacOS" / "Maramax" if resources else Path(sys.executable)
        return [str(executable), "--audio-worker"]
    return [sys.executable, "-m", "parakeet_dictation.main", "--audio-worker"]


class IsolatedAudioRecorder:
    """Set `device_name`, `prefer_builtin`, and `keep_warm_seconds` before a
    recording; read `frames`, `last_error`, `warm_start`, and `reset_count`
    after one. Everything else goes through the methods."""

    def __init__(self, recovery_dir: Path, command: list[str] | None = None,
                 start_timeout: float = 8.0, stop_timeout: float = 3.0):
        self._recovery_dir = recovery_dir
        # None records from Automatic; a name is never silently replaced.
        self.device_name: str | None = None
        self.prefer_builtin = True
        # Seconds the helper keeps the device open after a recording so the
        # next one starts instantly. 0 closes it immediately.
        self.keep_warm_seconds = 0
        self._command = command or worker_command()
        self._start_timeout = start_timeout
        self._stop_timeout = stop_timeout
        self._operation_lock = threading.Lock()
        # Listing runs in a one-shot helper of its own, so it never holds up a
        # recording: one listing at a time is all this lock guards.
        self._listing_lock = threading.Lock()
        self._data_lock = threading.Lock()
        self._ready = threading.Event()
        self._done = threading.Event()
        # Set while the helper is waiting for a request (idle, or holding a
        # warm stream); clear while it records or closes a device.
        self._accepting = threading.Event()
        self._process: subprocess.Popen[str] | None = None
        self._reader: threading.Thread | None = None
        self._spill: IO[bytes] | None = None
        # Whether the recovery file holds this capture's spill. When it holds
        # an earlier capture that could not be set aside, that file is the
        # earlier capture's only copy and must outlive this one.
        self._spill_is_ours = False
        # Whether every buffer of this capture reached the spill: a write
        # error stops spilling, leaving only the first part on disk.
        self._spill_is_whole = False
        self._closed = False
        self._closed_because = "Maramax is shutting down"
        self._overflows = 0
        self._started = False
        self._answered = False
        self._in_flight = False
        self.recording = False
        self.warm_start = False
        self.last_error: Exception | None = None
        # Helpers that had to be killed because they stopped answering.
        self.reset_count = 0
        self.frames: list[bytes] = []
        self.meter = CaptureMeter()
        # What Automatic resolved to at the last device enumeration.
        self.automatic_device_name: str | None = None

    def capture_snapshot(self) -> CaptureSnapshot:
        return replace(self.meter.snapshot(), overflow_count=self._overflows)

    # -- Helper process --

    def _popen(self):
        # Undecodable bytes are replaced rather than raised: an exception
        # would stop the thread that drains the helper's log.
        process = subprocess.Popen(self._command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                   stderr=subprocess.PIPE, text=True, errors="replace", bufsize=1)
        threading.Thread(target=self._relay_log, args=(process,), daemon=True).start()
        return process

    @staticmethod
    def _relay_log(process):
        """Copy the helper's log lines (its wedge and failover diagnostics)
        into the app's log. Drained to EOF so that a full pipe can never
        block a logging call inside the helper's audio paths; this thread
        owns the pipe and closes it."""
        assert process.stderr is not None
        try:
            with process.stderr:
                for line in process.stderr:
                    if line.strip():
                        logger.warning(f"Audio helper {process.pid}: {line.rstrip()}")
        except OSError as exc:
            logger.error(f"Stopped reading the log of audio helper {process.pid}: {exc}")

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
        try:
            process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            # SIGKILL cannot be refused, only delayed; callers hold locks
            # that must still be released.
            logger.error(f"Audio helper {process.pid} had not exited 2 s after being killed")

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
                if self._data_lock.acquire(timeout=0.5):
                    # Only waiting on a pipe that a slow-to-die helper still
                    # holds open. It is detached: it ignores whatever arrives
                    # for a helper that is no longer the current one.
                    self._data_lock.release()
                    return
                # Stuck inside a disk write, holding the lock every later
                # recording needs. Nothing can be recorded safely any more.
                self._closed = True
                self._closed_because = "the disk stopped responding while saving audio — restart Maramax"
                raise RuntimeError(f"Audio capture is unavailable: {self._closed_because}")
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
        except (OSError, RuntimeError) as exc:
            # Not fatal here: start() launches its own helper and reports
            # the failure to the user if that does not work either.
            logger.warning(f"Could not launch the standby audio helper: {exc}")
        finally:
            self._operation_lock.release()

    def release_device(self):
        """Close a microphone that is being kept open between dictations."""
        if not self._operation_lock.acquire(timeout=2):
            return
        try:
            process = self._process
            if process is not None and process.poll() is None and not self._in_flight:
                try:
                    self._write(process, request(Operation.RELEASE))
                except (OSError, ValueError):
                    pass  # The helper is gone, and its device with it.
        finally:
            self._operation_lock.release()

    def list_input_devices(self) -> list[str] | None:
        """The input devices' names in PortAudio's order, or None when they
        cannot be listed now (the caller keeps the list it has)."""
        if self.recording or not self._listing_lock.acquire(blocking=False):
            return None
        process = None
        try:
            if self.recording or self._closed:
                return None
            # A separate one-shot helper: a wedged enumeration must not
            # poison the standby process the next recording will use.
            process = self._popen()
            self._write(process, request(Operation.LIST, prefer_builtin=self.prefer_builtin))
            result: list[dict] = []
            answered = threading.Event()

            def read():
                assert process.stdout is not None
                try:
                    for line in process.stdout:
                        message = parse(line)
                        if message is not None and message.get("event") in (Event.DEVICES, Event.ERROR):
                            result.append(message)
                            return
                except OSError:
                    pass  # The pipe closed under us: reported below as "no answer".
                finally:
                    answered.set()

            threading.Thread(target=read, daemon=True).start()
            if not answered.wait(timeout=4):
                logger.warning("Could not list microphones: the audio helper did not answer within 4 s")
                return None
            if not result:
                logger.warning("Could not list microphones: the audio helper exited without answering")
                return None
            message = result[0]
            if message["event"] == Event.ERROR:
                logger.warning(f"Could not list microphones: {message.get('message')}")
                return None
            self.automatic_device_name = message.get("automatic")
            return list(message["devices"])
        except OSError as exc:
            logger.warning(f"Could not list microphones: {exc}")
            return None
        finally:
            if process is not None:
                self._kill(process)
                self._close_pipes(process)
            self._listing_lock.release()

    # -- Recording --

    def start(self, cancel: threading.Event | None = None) -> bool:
        """Open the microphone. `cancel` belongs to this one attempt: setting
        it abandons the wait, and it cannot leak into a later recording."""
        cancel = cancel or threading.Event()
        deadline = time.monotonic() + self._start_timeout
        if not self._operation_lock.acquire(timeout=min(4, self._start_timeout)):
            self.last_error = TimeoutError("Microphone is busy — try again")
            return False
        try:
            if self._closed:
                self.last_error = RuntimeError(self._closed_because)
                return False
            if self.recording:
                self.last_error = RuntimeError("A recording is already in progress")
                return False
            if cancel.is_set():
                self.last_error = RuntimeError("Microphone connection cancelled")
                return False
            self.preserve_recovery()
            record = request(Operation.RECORD, device=self.device_name, prefer_builtin=self.prefer_builtin)
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
                    self._write(process, record)
                except (OSError, ValueError):
                    pass  # Noticed below as a helper that exited without answering.
                while not self._ready.wait(timeout=0.02):
                    if cancel.is_set() or self._closed or time.monotonic() >= deadline:
                        break
                    if process.poll() is not None:
                        # Exited; its reader may still be delivering an answer.
                        self._ready.wait(timeout=0.2)
                        break
                if self._ready.is_set() and not self._answered and process.poll() is None:
                    # The reader saw the pipe close a moment before the exit
                    # status became available.
                    try:
                        process.wait(timeout=0.5)
                    except subprocess.TimeoutExpired:
                        pass  # Still running: handled below as an unanswered start.
                silent_exit = not self._answered and process.poll() is not None
                if cancel.is_set():
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
                    self.last_error = TimeoutError("Connection timed out — try again")
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
            except RuntimeError as stuck:
                logger.error(f"Could not clean up after a failed microphone start: {stuck}")
            return False
        finally:
            if self.last_error is not None:
                logger.warning(f"Microphone start failed: {self.last_error}")
            self._operation_lock.release()

    def _open_spill(self):
        self._spill = None
        self._spill_is_ours = False
        self._spill_is_whole = False
        if recovery.unkept_in_progress(self._recovery_dir):
            # An earlier capture could not be set aside as an unsaved
            # recording; opening the spill would truncate the only copy.
            logger.error("An earlier capture is still in the recovery file; this one is kept in memory only")
            return
        try:
            self._recovery_dir.mkdir(parents=True, exist_ok=True)
            self._spill = recovery.in_progress_path(self._recovery_dir).open("wb")
            self._spill_is_ours = True
            self._spill_is_whole = True
        except OSError as exc:
            # Memory capture and the final archive can still succeed.
            logger.warning(f"Recording will not be spilled to disk as it arrives: {exc}")

    def _abandon_start(self):
        self._in_flight = False
        # A helper that answered with an error has already let go of the
        # device and stays useful; one that never answered, or whose start
        # was cancelled mid-open, may be wedged in a driver call.
        if not self._answered or self._started:
            self._retire_process()
        self._close_spill()

    def _read(self, process):
        try:
            assert process.stdout is not None
            for line in process.stdout:
                message = parse(line)
                if message is None:
                    raise ValueError("The audio helper sent something that is not a message")
                if process is not self._process:
                    return  # Replaced helper: its late output belongs to no recording.
                kind = message.get("event")
                if kind == Event.AUDIO:
                    self._keep(base64.b64decode(message["pcm"], validate=True), message)
                elif kind == Event.READY:
                    self.meter.device_name = message["device"]
                    self.meter.mark_open()
                    self.warm_start = bool(message.get("warm"))
                    self._started = True
                    self._answered = True
                    # An IDLE from before this request (a warm stream closing
                    # as the request was sent) must not read as "free" now.
                    self._accepting.clear()
                    self._ready.set()
                elif kind == Event.RECONNECTING:
                    self.meter.set_reconnecting(True)
                elif kind == Event.DEVICE:
                    if message.get("reopened"):
                        self.meter.device_name = message.get("device") or self.meter.device_name
                    else:
                        # Nothing more will arrive; the app finishes with
                        # what was captured rather than showing a live meter.
                        self.last_error = RuntimeError(
                            f"The microphone disconnected and no other input could be opened: {message.get('error')}")
                    self.meter.set_reconnecting(False)
                elif kind == Event.ERROR:
                    self.last_error = RuntimeError(message.get("message", "Microphone failed"))
                    self._answered = True
                    self._ready.set()
                    self._done.set()
                elif kind == Event.DONE:
                    self._done.set()
                elif kind in (Event.IDLE, Event.WARM):
                    self._accepting.set()
                elif kind == Event.CLOSING:
                    self._accepting.clear()
        except Exception as exc:
            # Nothing reads this helper's output any more, so it must not be
            # handed another request: start() replaces a helper that is not
            # accepting rather than waiting out its whole start timeout.
            logger.error(f"Audio helper {process.pid} reader stopped: {exc!r}")
            if process is self._process:
                self._accepting.clear()
                if self._in_flight:
                    self.last_error = exc
        finally:
            if process is self._process:
                if self._in_flight and not self._done.is_set() and self.last_error is None:
                    self.last_error = RuntimeError("Microphone connection ended unexpectedly")
                self._ready.set()
                self._done.set()

    def _keep(self, pcm: bytes, message: dict) -> None:
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
                except OSError as exc:
                    # A full disk must not end the recording: the audio is
                    # still in memory and the archive may yet succeed.
                    logger.warning(f"Stopped spilling the recording to disk: {exc}")
                    self._spill_is_whole = False
                    spill, self._spill = self._spill, None
                    try:
                        spill.close()
                    except OSError:
                        pass  # Closing re-raises the same write error.

    def stop(self) -> bytes:
        with self._operation_lock:
            process = self._process
            if process is not None and self._in_flight:
                try:
                    self._write(process, request(Operation.STOP, keep_warm=self.keep_warm_seconds))
                except (OSError, ValueError):
                    if self.last_error is None:
                        self.last_error = RuntimeError("Microphone stopped responding; received audio was retained")
                # DONE follows the last audio on the same pipe, so the
                # buffer is complete once it arrives. The helper closes the
                # device afterwards, on its own time.
                if not self._done.wait(timeout=self._stop_timeout):
                    self.last_error = RuntimeError("Microphone stopped responding; received audio was retained")
                    # Killed while it is still the current helper, so its
                    # reader keeps the audio already in the pipe, up to EOF,
                    # before the helper is detached.
                    was_running = process.poll() is None
                    self._kill(process)
                    if self._reader is not None:
                        self._reader.join(timeout=2)
                    resets = self.reset_count
                    try:
                        self._retire_process()  # Counts the helper if it is somehow still running.
                    except RuntimeError as exc:
                        self.last_error = exc
                    if was_running and self.reset_count == resets:
                        self.reset_count += 1
                if self.last_error is not None:
                    logger.warning(f"Recording ended abnormally: {self.last_error}")
            self._in_flight = False
            self.recording = False
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

    @property
    def spill_holds_capture(self) -> bool:
        """Whether the recovery file holds the whole of this capture: False
        when it went without one because an earlier capture still occupied
        the file, or when a write error cut its spill short."""
        return self._spill_is_ours and self._spill_is_whole

    def preserve_recovery(self) -> bool:
        """Keep what was spilled as an unsaved recording of its own."""
        self._close_spill()
        # Whatever the rename does, the file no longer holds a spill this
        # recorder may delete: it is gone, or it is a capture not set aside.
        self._spill_is_ours = False
        return recovery.promote_in_progress(self._recovery_dir)

    def discard_recovery(self) -> None:
        """The capture is safe elsewhere (or unwanted): drop its spill. An
        earlier capture still in the recovery file is that capture's only
        copy, so it is set aside once more instead of deleted."""
        self._close_spill()
        if self._spill_is_ours:
            self._spill_is_ours = False
            recovery.discard_in_progress(self._recovery_dir)
        else:
            recovery.promote_in_progress(self._recovery_dir)

    def cleanup(self):
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
                    pass  # It is killed just below either way.
            resets = self.reset_count
            try:
                self._retire_process()
            except RuntimeError as exc:
                logger.error(f"Audio helper shutdown was incomplete: {exc}")
            self.reset_count = resets  # Shutting down a healthy helper is not a reset.
