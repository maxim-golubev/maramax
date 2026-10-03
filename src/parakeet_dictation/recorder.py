"""In-process PyAudio capture with bounded PortAudio teardown; runs inside the audio helper."""

from __future__ import annotations

import ctypes
import threading
import time
from typing import Any

import pyaudio

from .audio_format import CHANNELS, SAMPLE_RATE
from .capture import CaptureMeter, CaptureSnapshot
from .helper_protocol import InputDevice
from .logger_config import logger

FRAMES_PER_BUFFER = 512


def lid_closed() -> bool:
    """True while a laptop lid is shut. Apple silicon disconnects the built-in
    microphone in hardware then, so it enumerates but only delivers silence."""
    try:
        iokit = ctypes.cdll.LoadLibrary("/System/Library/Frameworks/IOKit.framework/IOKit")
        cf = ctypes.cdll.LoadLibrary("/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation")
        iokit.IOServiceMatching.restype = ctypes.c_void_p
        iokit.IOServiceMatching.argtypes = [ctypes.c_char_p]
        iokit.IOServiceGetMatchingService.restype = ctypes.c_uint32
        iokit.IOServiceGetMatchingService.argtypes = [ctypes.c_uint32, ctypes.c_void_p]
        iokit.IORegistryEntryCreateCFProperty.restype = ctypes.c_void_p
        iokit.IORegistryEntryCreateCFProperty.argtypes = [
            ctypes.c_uint32, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint32,
        ]
        iokit.IOObjectRelease.argtypes = [ctypes.c_uint32]
        cf.CFStringCreateWithCString.restype = ctypes.c_void_p
        cf.CFStringCreateWithCString.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_uint32]
        cf.CFGetTypeID.restype = ctypes.c_ulong
        cf.CFGetTypeID.argtypes = [ctypes.c_void_p]
        cf.CFBooleanGetTypeID.restype = ctypes.c_ulong
        cf.CFBooleanGetValue.restype = ctypes.c_bool
        cf.CFBooleanGetValue.argtypes = [ctypes.c_void_p]
        cf.CFRelease.argtypes = [ctypes.c_void_p]

        # IOServiceGetMatchingService consumes the matching dictionary.
        service = iokit.IOServiceGetMatchingService(0, iokit.IOServiceMatching(b"IOPMrootDomain"))
        if not service:
            return False
        key = cf.CFStringCreateWithCString(None, b"AppleClamshellState", 0x08000100)  # UTF-8
        try:
            value = iokit.IORegistryEntryCreateCFProperty(service, key, None, 0)
        finally:
            cf.CFRelease(key)
            iokit.IOObjectRelease(service)
        if not value:
            return False  # Desktops have no clamshell state.
        try:
            return cf.CFGetTypeID(value) == cf.CFBooleanGetTypeID() and bool(cf.CFBooleanGetValue(value))
        finally:
            cf.CFRelease(value)
    except Exception as exc:
        # An unreadable lid state is treated as open: the preference for the
        # built-in microphone then behaves as it did before lids were checked.
        logger.debug(f"Lid state unavailable: {exc}")
        return False


class _PropertyAddress(ctypes.Structure):
    """CoreAudio's AudioObjectPropertyAddress."""
    _fields_ = [("selector", ctypes.c_uint32), ("scope", ctypes.c_uint32), ("element", ctypes.c_uint32)]


def default_input_device() -> int | None:
    """CoreAudio's identifier of the system default input as it is now, or
    None when it cannot be read. PortAudio cannot tell: it fixes its device
    list, default included, when it initializes."""
    try:
        core_audio = ctypes.cdll.LoadLibrary("/System/Library/Frameworks/CoreAudio.framework/CoreAudio")
        get = core_audio.AudioObjectGetPropertyData
        get.restype = ctypes.c_int32
        get.argtypes = [ctypes.c_uint32, ctypes.POINTER(_PropertyAddress), ctypes.c_uint32, ctypes.c_void_p,
                        ctypes.POINTER(ctypes.c_uint32), ctypes.c_void_p]
        # kAudioHardwarePropertyDefaultInputDevice of kAudioObjectSystemObject,
        # global scope, main element.
        address = _PropertyAddress(int.from_bytes(b"dIn ", "big"), int.from_bytes(b"glob", "big"), 0)
        device = ctypes.c_uint32(0)
        size = ctypes.c_uint32(ctypes.sizeof(device))
        status = get(1, ctypes.byref(address), 0, None, ctypes.byref(size), ctypes.byref(device))
        if status != 0:
            raise OSError(f"AudioObjectGetPropertyData returned {status}")
        return device.value
    except Exception as exc:
        # Unknown compares equal to unknown, so a kept-warm stream is then
        # reused as it was before the default was checked.
        logger.warning(f"Default input device unavailable: {exc}")
        return None


class AudioRecorder:
    def __init__(self, device_name: str | None = None, prefer_builtin: bool = True):
        # None records from Automatic; a name is resolved strictly.
        self.device_name = device_name
        self.prefer_builtin = prefer_builtin
        # Even PortAudio initialization can block during a route change.
        # Defer it to the first background enumeration or capture request.
        self.audio: Any = None
        self.frames: list[bytes] = []
        self.recording = False
        self.last_error: Exception | None = None
        # Incremented per start() and reopen(); a stream whose generation
        # is stale can no longer write into the recording.
        self.start_generation = 0
        self._recording_thread: threading.Thread | None = None
        self._stream: Any = None
        self._state_lock = threading.Lock()
        self._stream_lock = threading.Lock()
        # Serializes use of the PyAudio instance (open/enumerate/reinit) so a
        # background device refresh can't tear it down mid-open.
        self._audio_lock = threading.Lock()
        self._cleaned_up = False
        self._released_cleanly = False
        self.meter = CaptureMeter()
        self.abandoned_sessions = 0

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
        except Exception as exc:
            # Nothing to recover: the replacement instance below is what matters.
            logger.warning(f"PortAudio did not terminate cleanly: {exc}")
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
                default_index = -1  # No default input: no entry is marked.

            devices: list[InputDevice] = []
            for i in range(self.audio.get_device_count()):
                try:
                    info = self.audio.get_device_info_by_index(i)
                except (IOError, OSError):
                    continue  # A device that vanished mid-enumeration.
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
        if self.device_name is None:
            return self._find_builtin_index() if self.prefer_builtin else None
        for i in range(self.audio.get_device_count()):
            try:
                info = self.audio.get_device_info_by_index(i)
            except (IOError, OSError):
                continue
            if info["name"] == self.device_name and info.get("maxInputChannels", 0) > 0:
                return i
        # A locked device disappearing must not silently choose another mic.
        raise OSError(f"Selected microphone disconnected: {self.device_name}")

    def capture_snapshot(self) -> CaptureSnapshot:
        return self.meter.snapshot()

    def automatic_device_name(self) -> str | None:
        """The input Automatic mode would open right now, for display."""
        if not self._audio_lock.acquire(timeout=3.0):
            return None
        try:
            if self.audio is None:
                return None
            index = self._find_builtin_index() if self.prefer_builtin else None
            if index is None:
                index = int(self.audio.get_default_input_device_info()["index"])
            return str(self.audio.get_device_info_by_index(index)["name"])
        except (IOError, OSError, KeyError, ValueError):
            return None  # No usable input right now; the picker shows plain "Automatic".
        finally:
            self._audio_lock.release()

    def _find_builtin_index(self) -> int | None:
        """Prefer the Mac’s input without changing the system's output or
        opening a Bluetooth headset microphone unless explicitly selected."""
        if lid_closed():
            # The built-in microphone is cut off in hardware; preferring it
            # would record silence. Fall through to the system default.
            return None
        for i in range(self.audio.get_device_count()):
            try:
                info = self.audio.get_device_info_by_index(i)
            except (IOError, OSError):
                continue
            name = str(info.get("name", "")).lower()
            if info.get("maxInputChannels", 0) > 0 and (
                (("macbook" in name or "imac" in name) and "microphone" in name)
                or name == "built-in microphone"
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

        self.meter = CaptureMeter()

        # Rebuild the audio session at each start (~40 ms): PortAudio
        # snapshots the device list at init, so a reused session silently
        # records from a stale default device after AirPods reconnect.
        # The lock acquire is bounded; native calls themselves may still
        # wedge, which is why this runs in a process the app can replace.
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

        self.meter.mark_open()
        self._start_record_loop()
        return True

    def _start_record_loop(self) -> None:
        thread = threading.Thread(
            target=self._record_loop,
            args=(self._stream, self.start_generation),
            daemon=True,
        )
        with self._state_lock:
            self._recording_thread = thread
        thread.start()

    def reopen(self) -> bool:
        """Continue the current recording on whichever input is available
        now (the device vanished or stopped delivering). Captured audio and
        measurements carry over; an explicitly selected microphone that is
        gone still fails rather than silently switching."""
        with self._state_lock:
            if self._cleaned_up or not self.recording or self.abandoned_sessions >= 2:
                return False
            previous = self._recording_thread
            self._recording_thread = None
            # Retires the old callback and record loop; they keep only
            # their own stream reference.
            self.start_generation += 1
        if previous is not None:
            previous.join(timeout=2.0)

        if not self._audio_lock.acquire(timeout=5.0):
            return False
        try:
            self._reinit_audio()
            if self.abandoned_sessions:
                # The old session could not be shut down, so PortAudio kept
                # its device list: it still names the device that vanished.
                raise OSError("the audio session could not be rebuilt after the device stopped responding")
            self._open_stream()
        except Exception as exc:
            logger.error(f"Microphone could not be reopened: {exc}")
            with self._state_lock:
                self.last_error = exc
            self._close_stream(timeout=2.0)
            return False
        finally:
            self._audio_lock.release()

        self._start_record_loop()
        return True

    def rearm(self) -> bool:
        """Begin a new recording on a stream that was kept open. The buffer
        is emptied and the meter restarted in place, because the live
        callback holds both objects."""
        with self._state_lock:
            if self._cleaned_up or not self.recording or self._stream is None:
                return False
            self.last_error = None
        del self.frames[:]
        self.meter.restart()
        return True

    def stream_active(self) -> bool:
        stream = self._stream
        try:
            return stream is not None and bool(stream.is_active())
        except Exception:
            return False  # A stream that cannot answer is not usable.

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
                # The recording thread is stuck inside PortAudio. Force a
                # close on a sacrificial thread: PortAudio calls cannot be
                # interrupted, so even the forced close may wedge, and it
                # must not take this caller down with it. The captured
                # frames are safe in memory regardless.
                logger.warning("Recording thread did not stop in time, forcing stream close")
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

    def cleanup(self) -> bool:
        """Release the device. False when PortAudio could not be shut down
        completely: it then stays initialized in this process with its
        device list frozen, so the process must not record again."""
        with self._state_lock:
            if self._cleaned_up:
                return self._released_cleanly
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
                self._released_cleanly = self.abandoned_sessions == 0
            except Exception as exc:
                logger.warning(f"PortAudio did not terminate cleanly: {exc}")
            finally:
                self._audio_lock.release()
        return self._released_cleanly

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
        meter = self.meter

        def callback(in_data, frame_count, time_info, status_flags):
            del frame_count, time_info

            if self.start_generation == generation and self.is_recording():
                frames.append(in_data)
                meter.feed(in_data, overflow=bool(status_flags & pyaudio.paInputOverflow))
                return (None, pyaudio.paContinue)

            return (None, pyaudio.paComplete)

        with self._stream_lock:
            # PyAudio starts an input stream on open. Avoid a second start
            # during Bluetooth route negotiation.
            self._stream = self.audio.open(
                format=pyaudio.paInt16,
                channels=CHANNELS,
                rate=SAMPLE_RATE,
                input=True,
                frames_per_buffer=FRAMES_PER_BUFFER,
                stream_callback=callback,
                input_device_index=device_index,
            )

    def _record_loop(self, stream, generation: int) -> None:
        # These references are captured BEFORE the thread is started. A loop
        # delayed by scheduling/driver locks cannot adopt a newer recording.
        if stream is None:
            return
        try:
            while stream.is_active():
                if generation != self.start_generation or not self.is_recording():
                    break
                time.sleep(0.01)
        except Exception as exc:
            logger.error(f"Microphone stream error: {exc}")
            with self._state_lock:
                if generation == self.start_generation:
                    self.last_error = exc
        finally:
            self._close_stream(expected=stream)

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

            # A stream whose device vanished raises from both calls; either
            # way it is no longer ours to use, which is all that is wanted.
            try:
                stream.stop_stream()
            except Exception as exc:
                logger.debug(f"stop_stream on a dying stream: {exc}")

            try:
                stream.close()
            except Exception as exc:
                logger.debug(f"close on a dying stream: {exc}")
            return True
        finally:
            lock.release()
