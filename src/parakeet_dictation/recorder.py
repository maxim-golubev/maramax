"""In-process PyAudio capture with bounded PortAudio teardown; runs inside the audio helper."""

from __future__ import annotations

import threading
import time
from pathlib import Path
from typing import IO, Any, NamedTuple

import pyaudio

from . import recovery
from .capture import CaptureMeter, CaptureSnapshot
from .logger_config import setup_logging
from .paths import app_support_dir

logger = setup_logging()


class InputDevice(NamedTuple):
    device_index: int
    name: str
    is_default: bool


class AudioRecorder:
    def __init__(self, recovery_dir: Path | None = None, prefer_builtin: bool = True):
        # Even PortAudio initialization can block during a route change.
        # Defer it to the first background enumeration or capture request.
        self.audio: Any = None
        self.format = pyaudio.paInt16
        self.channels = 1
        self.rate = 16000
        self.chunk = 512
        self.frames: list[bytes] = []
        self.recording = False
        self.last_error: Exception | None = None
        self.first_frame_event = threading.Event()
        # Set when frames contain actual signal (Bluetooth mics deliver
        # pure-zero frames for 1-2s while switching into headset mode).
        self.signal_event = threading.Event()
        # Incremented per start(); lets watchers detect they span recordings.
        self.start_generation = 0
        self._recording_thread: threading.Thread | None = None
        self._stream: Any = None
        self._state_lock = threading.Lock()
        self._stream_lock = threading.Lock()
        # Serializes use of the PyAudio instance (open/enumerate/reinit) so a
        # background device refresh can't tear it down mid-open.
        self._audio_lock = threading.Lock()
        self._cleaned_up = False
        self._selected_device_name: str | None = None
        self.prefer_builtin = prefer_builtin
        self.meter = CaptureMeter(self.rate)
        self.abandoned_sessions = 0
        # Crash insurance: the capture is spilled to disk while recording so
        # a hang, crash, or failed transcription can't lose a long dictation.
        self._recovery_dir = recovery_dir or app_support_dir()
        self._recovery_file: IO[bytes] | None = None
        self._recovery_flushed = 0
        # Guards the spill handle/cursor between the record loop and the
        # app-side preserve/discard calls (never held around Pa calls).
        self._recovery_lock = threading.Lock()

    @property
    def recovery_dir(self) -> Path:
        return self._recovery_dir

    def set_device(self, name: str | None) -> None:
        self._selected_device_name = name

    def get_selected_device_name(self) -> str | None:
        return self._selected_device_name

    def _reinit_audio(self) -> None:
        # Callers must hold _audio_lock. Close any live stream first:
        # Pa_Terminate() frees open streams behind the back of whoever
        # later calls close() on them (malloc abort / double free).
        if not self._close_stream(timeout=2.0):
            # A wedged Pa_StopStream (Bluetooth route change) still holds the
            # stream lock. Terminating PortAudio now would free that stream
            # under the stuck call and abort the process — abandon the old
            # session instead (leaks a handle, but stays alive and usable).
            self._abandon_stream_session()
            return
        try:
            if self.audio is not None:
                self.audio.terminate()
        except Exception:
            pass
        self.audio = pyaudio.PyAudio()

    def list_input_devices(self) -> list[InputDevice] | None:
        """Input devices, or None when the audio session is busy (callers
        should keep their current list rather than show an empty one)."""
        if not self._audio_lock.acquire(timeout=3.0):
            logger.warning("Audio session busy; skipping device enumeration")
            return None
        try:
            if not self.is_recording():
                self._reinit_audio()

            try:
                default_index = self.audio.get_default_input_device_info()["index"]
            except (IOError, OSError):
                default_index = -1

            devices: list[InputDevice] = []
            for i in range(self.audio.get_device_count()):
                try:
                    info = self.audio.get_device_info_by_index(i)
                except (IOError, OSError):
                    continue
                if info.get("maxInputChannels", 0) > 0:
                    devices.append(InputDevice(
                        device_index=i,
                        name=info["name"],
                        is_default=(i == default_index),
                    ))
            return devices
        finally:
            self._audio_lock.release()

    def _resolve_device_index(self) -> int | None:
        if self._selected_device_name is None:
            return self._find_builtin_index() if self.prefer_builtin else None
        for i in range(self.audio.get_device_count()):
            try:
                info = self.audio.get_device_info_by_index(i)
            except (IOError, OSError):
                continue
            if info["name"] == self._selected_device_name and info.get("maxInputChannels", 0) > 0:
                return i
        # A locked device disappearing must not silently choose another mic.
        raise OSError(f"Selected microphone disconnected: {self._selected_device_name}")

    def capture_snapshot(self) -> CaptureSnapshot:
        return self.meter.snapshot()

    def _find_builtin_index(self) -> int | None:
        """Prefer the Mac's input without changing the system's output or
        opening a Bluetooth headset microphone unless explicitly selected."""
        for i in range(self.audio.get_device_count()):
            try:
                info = self.audio.get_device_info_by_index(i)
            except (IOError, OSError):
                continue
            name = str(info.get("name", "")).lower()
            if info.get("maxInputChannels", 0) > 0 and (
                ("macbook" in name and "microphone" in name) or name == "built-in microphone"
            ):
                return i
        return None

    def start(self) -> bool:
        with self._state_lock:
            if self._cleaned_up or self.recording:
                return False
            if self.abandoned_sessions >= 2:
                self.last_error = RuntimeError("Microphone driver repeatedly stalled — restart Maramax")
                return False

            self.frames = []
            self.recording = True
            self.last_error = None
            self.start_generation += 1

        self.first_frame_event = threading.Event()
        self.signal_event = threading.Event()
        self.meter = CaptureMeter(self.rate)

        # Rebuild the audio session at each start (~85ms): PortAudio
        # snapshots the device list at init, so a reused session silently
        # records from a stale default device after AirPods reconnect.
        # Capture starts on a worker; the UI stays in "Connecting" until
        # input arrives, so a driver open cannot freeze the main loop.
        # The lock acquire is bounded; native calls themselves may still
        # wedge and require an application restart.
        if not self._audio_lock.acquire(timeout=5.0):
            exc: Exception = TimeoutError("audio session busy")
            logger.error("Microphone start failed: audio session lock timeout")
            with self._state_lock:
                self.recording = False
                self.last_error = exc
            return False
        try:
            self._reinit_audio()
            self._open_stream()
        except Exception as exc:
            logger.error(f"Microphone start failed: {exc}")
            with self._state_lock:
                self.recording = False
                self.last_error = exc
            self._close_stream(timeout=2.0)
            return False
        finally:
            self._audio_lock.release()

        self._open_recovery_file()
        thread = threading.Thread(
            target=self._record_loop,
            args=(self._stream, self._recovery_file, self.start_generation),
            daemon=True,
        )
        with self._state_lock:
            self._recording_thread = thread
        thread.start()
        return True

    def stop(self) -> bytes:
        with self._state_lock:
            if not self.recording:
                return b""
            self.recording = False
            thread = self._recording_thread
            self._recording_thread = None

        if thread is not None:
            thread.join(timeout=5.0)
            if thread.is_alive():
                # The recording thread is stuck inside PortAudio, possibly
                # before its finalize ran — spill the tail of the capture
                # first so the recovery file is complete. Then force a close
                # on a sacrificial thread: PortAudio calls cannot be
                # interrupted, so even the forced close may wedge, and it
                # must not take this caller down with it. The captured
                # frames are safe in memory regardless.
                logger.warning("Recording thread did not stop in time, forcing stream close")
                self._flush_recovery(self._recovery_file)
                closed: list[bool] = []
                closer = threading.Thread(
                    target=lambda: closed.append(self._close_stream(timeout=2.0)),
                    daemon=True,
                )
                closer.start()
                closer.join(timeout=6.0)
                if not closed or not closed[0]:
                    logger.error("Audio stream wedged; abandoning audio session")
                    self._abandon_stream_session()

        audio_data = b"".join(self.frames)
        self.frames = []
        return audio_data

    def is_recording(self) -> bool:
        with self._state_lock:
            return self.recording

    def cleanup(self) -> None:
        with self._state_lock:
            if self._cleaned_up:
                return
            self._cleaned_up = True

        if self.is_recording():
            self.stop()

        # Bounded everywhere: quitting must never hang on a wedged stream.
        # If the close failed, a stream is still open (possibly mid-wedge) —
        # terminating would free it under the stuck call and abort the
        # process; leaking at exit is the safe choice.
        if self._close_stream(timeout=2.0) and self._audio_lock.acquire(timeout=3.0):
            try:
                if self.audio is not None:
                    self.audio.terminate()
            except Exception:
                pass
            finally:
                self._audio_lock.release()

    def sample_width(self) -> int:
        return pyaudio.get_sample_size(self.format)

    def _abandon_stream_session(self) -> None:
        """A wedged holder owns the current stream lock and may never
        release it. Give future streams a fresh lock, drop the zombie
        stream reference (retrying its close would wedge the caller too),
        and retire the whole PyAudio session WITHOUT terminating it —
        Pa_Terminate would free the wedged stream under the stuck call and
        abort the process. The old session leaks; the replacement works.
        The zombie thread keeps the old lock and only ever touches its own
        local stream reference."""
        if self.abandoned_sessions >= 2:
            raise RuntimeError("Microphone driver repeatedly stalled — restart Maramax")
        logger.warning("Abandoning wedged audio session")
        self.abandoned_sessions += 1
        self._stream_lock = threading.Lock()
        with self._stream_lock:
            self._stream = None
        self.audio = pyaudio.PyAudio()

    def _open_stream(self) -> None:
        device_index = self._resolve_device_index()
        if device_index is None:
            device_index = int(self.audio.get_default_input_device_info()["index"])
        device_info = self.audio.get_device_info_by_index(device_index)
        self.meter.device_name = str(device_info["name"])

        # Captured, not read from self: if this stream is ever abandoned
        # (wedged close) and later comes back to life, its callback must not
        # write into a newer recording's buffers or vouch for its mic.
        generation = self.start_generation
        frames = self.frames
        first_frame_event = self.first_frame_event
        signal_event = self.signal_event
        meter = self.meter

        def callback(in_data, frame_count, time_info, status_flags):
            del frame_count, time_info

            if self.start_generation == generation and self.is_recording():
                frames.append(in_data)
                meter.feed(in_data, overflow=bool(status_flags & pyaudio.paInputOverflow))
                if in_data:
                    first_frame_event.set()
                if not signal_event.is_set() and any(in_data):
                    # A live mic always has a noise floor; exact digital
                    # silence means the route (e.g. a Bluetooth headset
                    # switching into mic mode) isn't delivering audio yet.
                    signal_event.set()
                return (None, pyaudio.paContinue)

            return (None, pyaudio.paComplete)

        kwargs = dict(
            format=self.format,
            channels=self.channels,
            rate=self.rate,
            input=True,
            frames_per_buffer=self.chunk,
            stream_callback=callback,
        )
        if device_index is not None:
            kwargs["input_device_index"] = device_index

        with self._stream_lock:
            self._stream = self.audio.open(**kwargs)
            # PyAudio starts an input stream by default. Avoid a second start
            # during Bluetooth route negotiation.

    def _record_loop(self, stream, recovery_handle: IO[bytes] | None, generation: int) -> None:
        # These references are captured BEFORE the thread is started. A loop
        # delayed by scheduling/driver locks cannot adopt a newer recording.
        if stream is None:
            self._finalize_recovery(recovery_handle)
            return

        last_flush = time.monotonic()
        try:
            while stream.is_active():
                if generation != self.start_generation or not self.is_recording():
                    break
                now = time.monotonic()
                if now - last_flush >= 1.0:
                    self._flush_recovery(recovery_handle)
                    last_flush = now
                time.sleep(0.01)
        except Exception as exc:
            logger.error(f"Microphone stream error: {exc}")
            with self._state_lock:
                if generation == self.start_generation:
                    self.last_error = exc
        finally:
            # Recovery file first: even if the stream close wedges below,
            # the captured audio is already complete on disk.
            self._finalize_recovery(recovery_handle)
            self._close_stream(expected=stream)

    def _open_recovery_file(self) -> None:
        with self._recovery_lock:
            self._close_current_recovery_handle()
            self._recovery_flushed = 0
            try:
                self._recovery_dir.mkdir(parents=True, exist_ok=True)
                self._recovery_file = open(recovery.in_progress_path(self._recovery_dir), "wb")
            except OSError as exc:
                self._recovery_file = None
                logger.warning(f"Recording recovery file unavailable: {exc}")

    def _flush_recovery(self, handle: IO[bytes] | None) -> None:
        with self._recovery_lock:
            if handle is None or handle is not self._recovery_file:
                # A stale (zombie) handle must not touch the current spill.
                return
            frames = self.frames
            end = len(frames)
            if end <= self._recovery_flushed:
                return
            try:
                handle.write(b"".join(frames[self._recovery_flushed:end]))
                handle.flush()
                self._recovery_flushed = end
            except (OSError, ValueError) as exc:
                # ValueError: the handle was closed under us (e.g. the app
                # discarded the recovery file while a wedged stop lingered).
                logger.warning(f"Recovery write failed: {exc}")
                self._close_current_recovery_handle()

    def _finalize_recovery(self, handle: IO[bytes] | None) -> None:
        self._flush_recovery(handle)
        with self._recovery_lock:
            if handle is not None and handle is self._recovery_file:
                self._close_current_recovery_handle()
            elif handle is not None:
                # Stale handle from an abandoned recording — close just it.
                try:
                    handle.close()
                except (OSError, ValueError):
                    pass

    def _close_recovery(self) -> None:
        with self._recovery_lock:
            self._close_current_recovery_handle()

    def _close_current_recovery_handle(self) -> None:
        # Callers must hold _recovery_lock.
        handle = self._recovery_file
        self._recovery_file = None
        if handle is None:
            return
        try:
            handle.close()
        except (OSError, ValueError):
            pass

    def discard_recovery(self) -> None:
        """The capture was transcribed (or deliberately dropped) — remove
        the in-progress spill file."""
        self._close_recovery()
        recovery.discard_in_progress(self._recovery_dir)

    def preserve_recovery(self, only_if_larger: bool = False) -> bool:
        """Keep the current capture on disk as the recoverable last
        recording. True when a recoverable file is in place. Flushes any
        frames the record loop never reached (wedged mid-loop) first."""
        self._flush_recovery(self._recovery_file)
        self._close_recovery()
        return recovery.promote_in_progress(self._recovery_dir, only_if_larger=only_if_larger)

    def has_recoverable_recording(self) -> bool:
        return recovery.has_last_recording(self._recovery_dir)

    def load_recoverable_recording(self) -> bytes | None:
        """Raw PCM of the preserved recording — worker threads only (reads
        the whole file into memory)."""
        return recovery.load_last_recording(self._recovery_dir)

    def discard_recoverable_recording(self) -> None:
        recovery.discard_last_recording(self._recovery_dir)

    def _close_stream(self, timeout: float | None = None, expected: Any = None) -> bool:
        # stop+close stay inside the lock: if another thread terminates the
        # audio session while we're between the two calls, PortAudio frees
        # the stream under us and close() aborts the process.
        # A wedged Pa_StopStream can hold this lock indefinitely — callers
        # that must not block pass a timeout (bounds the acquire only; the
        # Pa calls themselves cannot be interrupted) and abandon the stream
        # when it can't be acquired.
        # `expected` (the record loop's own stream) prevents a slow closer
        # from tearing down a *newer* recording's stream: on mismatch it
        # closes only its own handle.
        lock = self._stream_lock
        if timeout is None:
            lock.acquire()
        elif not lock.acquire(timeout=timeout):
            return False
        try:
            stream = self._stream
            if expected is not None and stream is not expected:
                # Our stream was already replaced or abandoned; its session
                # is never terminated, so closing the local handle is safe.
                stream = expected
            else:
                self._stream = None
            if stream is None:
                return True

            try:
                stream.stop_stream()
            except Exception:
                pass

            try:
                stream.close()
            except Exception:
                pass
            return True
        finally:
            lock.release()
