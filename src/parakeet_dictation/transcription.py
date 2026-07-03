from __future__ import annotations

import gc
import os
import shutil
import subprocess
import tempfile
import threading
import time
import wave
from collections.abc import Callable
from pathlib import Path
from typing import IO, Any, NamedTuple

import mlx.core as mx
import numpy as np
import pyaudio
from parakeet_mlx import from_pretrained

from . import recovery
from .logger_config import setup_logging
from .paths import app_support_dir

logger = setup_logging()

FFMPEG_TIMEOUT_SECONDS = 120
FFMPEG_CANDIDATES = (
    "ffmpeg",
    "/opt/homebrew/bin/ffmpeg",
    "/usr/local/bin/ffmpeg",
    "/usr/bin/ffmpeg",
)


class TranscriptionError(RuntimeError):
    pass


class InputDevice(NamedTuple):
    device_index: int
    name: str
    is_default: bool


class AudioRecorder:
    def __init__(self, recovery_dir: Path | None = None):
        self.audio = pyaudio.PyAudio()
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
            return None
        for i in range(self.audio.get_device_count()):
            try:
                info = self.audio.get_device_info_by_index(i)
            except (IOError, OSError):
                continue
            if info["name"] == self._selected_device_name and info.get("maxInputChannels", 0) > 0:
                return i
        logger.warning(f"Input device '{self._selected_device_name}' not found, using system default")
        return None

    def _find_builtin_index(self) -> int | None:
        """Used only for warm-up: initializing the capture stack on the
        built-in mic avoids flipping Bluetooth headphones out of
        high-quality playback mode at app launch."""
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

    def warm_up(self) -> None:
        """Open and close an input stream once so CoreAudio's capture stack
        is initialized — the first open after process start costs seconds,
        subsequent opens are fast. Run in the background at app launch."""
        if self.is_recording() or self._cleaned_up:
            return
        try:
            with self._audio_lock:
                kwargs = dict(
                    format=self.format,
                    channels=self.channels,
                    rate=self.rate,
                    input=True,
                    frames_per_buffer=self.chunk,
                )
                # Warm up on the built-in mic when present: it initializes the
                # capture stack without flipping Bluetooth headphones out of
                # high-quality playback mode at app launch.
                device_index = self._find_builtin_index()
                if device_index is None:
                    device_index = self._resolve_device_index()
                if device_index is not None:
                    kwargs["input_device_index"] = device_index
                stream = self.audio.open(**kwargs)
                stream.stop_stream()
                stream.close()
            logger.info("Microphone warmed up")
        except Exception as exc:
            logger.warning(f"Microphone warm-up failed: {exc}")

    def start(self) -> bool:
        with self._state_lock:
            if self._cleaned_up or self.recording:
                return False

            self.frames = []
            self.recording = True
            self.last_error = None
            self.start_generation += 1

        self.first_frame_event = threading.Event()
        self.signal_event = threading.Event()

        # Rebuild the audio session at each start (~85ms): PortAudio
        # snapshots the device list at init, so a reused session silently
        # records from a stale default device after AirPods reconnect.
        # No speech is lost — the "Recording…" indicator only shows once
        # audio actually flows (signal_event).
        # Bounded acquire: start() runs on the main thread, and a wedged
        # audio session must fail the start, never beachball the app.
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
        thread = threading.Thread(target=self._record_loop, daemon=True)
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
                self.audio.terminate()
            except Exception:
                pass
            finally:
                self._audio_lock.release()

    def sample_width(self) -> int:
        return self.audio.get_sample_size(self.format)

    def _abandon_stream_session(self) -> None:
        """A wedged holder owns the current stream lock and may never
        release it. Give future streams a fresh lock, drop the zombie
        stream reference (retrying its close would wedge the caller too),
        and retire the whole PyAudio session WITHOUT terminating it —
        Pa_Terminate would free the wedged stream under the stuck call and
        abort the process. The old session leaks; the replacement works.
        The zombie thread keeps the old lock and only ever touches its own
        local stream reference."""
        logger.warning("Abandoning wedged audio session")
        self._stream_lock = threading.Lock()
        with self._stream_lock:
            self._stream = None
        self.audio = pyaudio.PyAudio()

    def _open_stream(self) -> None:
        device_index = self._resolve_device_index()

        # Captured, not read from self: if this stream is ever abandoned
        # (wedged close) and later comes back to life, its callback must not
        # write into a newer recording's buffers or vouch for its mic.
        generation = self.start_generation
        frames = self.frames
        first_frame_event = self.first_frame_event
        signal_event = self.signal_event

        def callback(in_data, frame_count, time_info, status_flags):
            del frame_count, time_info, status_flags

            if self.start_generation == generation and self.is_recording():
                frames.append(in_data)
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
            self._stream.start_stream()

    def _record_loop(self) -> None:
        with self._stream_lock:
            stream = self._stream
        # Captured like the stream: a zombie loop from an abandoned session
        # must never flush into or close a newer recording's spill file.
        recovery_handle = self._recovery_file
        if stream is None:
            self._finalize_recovery(recovery_handle)
            return

        last_flush = time.monotonic()
        try:
            while stream.is_active():
                if not self.is_recording():
                    break
                now = time.monotonic()
                if now - last_flush >= 1.0:
                    self._flush_recovery(recovery_handle)
                    last_flush = now
                time.sleep(0.01)
        except Exception as exc:
            logger.error(f"Microphone stream error: {exc}")
            with self._state_lock:
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


class ParakeetTranscriber:
    def __init__(self, model_id: str = "mlx-community/parakeet-tdt-0.6b-v2"):
        self.model_id = model_id
        self.model = None
        self.load_error: Exception | None = None
        self.ready_event = threading.Event()
        self._loader = threading.Thread(target=self._load_model, daemon=True)
        self._loader.start()

    def _load_model(self) -> None:
        try:
            self.model = from_pretrained(self.model_id)
            self._warm_model()
            logger.info("Parakeet model loaded successfully")
        except Exception as exc:
            self.load_error = exc
            logger.error(f"Error loading Parakeet model: {exc}")
        finally:
            self.ready_event.set()

    def _warm_model(self) -> None:
        assert self.model is not None
        silence = np.zeros(int(0.3 * 16000), dtype=np.int16).tobytes()
        temp_path = write_wav_file(silence, channels=1, sample_width=2, rate=16000)
        try:
            self.model.transcribe(temp_path)
            gc.collect()
            mx.clear_cache()
        finally:
            try:
                os.unlink(temp_path)
            except OSError:
                pass

    def wait_until_ready(self) -> None:
        self.ready_event.wait()
        if self.load_error is not None:
            raise TranscriptionError(f"Model failed to load: {self.load_error}") from self.load_error
        if self.model is None:
            raise TranscriptionError("Model failed to initialize")

    def is_ready(self) -> bool:
        return self.ready_event.is_set() and self.model is not None and self.load_error is None

    def transcribe_pcm(
        self,
        pcm_bytes: bytes,
        channels: int,
        sample_width: int,
        rate: int,
        progress_callback: Callable | None = None,
    ) -> str:
        if not pcm_bytes:
            return ""

        self.wait_until_ready()
        temp_path = write_wav_file(pcm_bytes, channels=channels, sample_width=sample_width, rate=rate)
        try:
            return self._transcribe_path(temp_path, progress_callback=progress_callback)
        finally:
            try:
                os.unlink(temp_path)
            except OSError:
                pass

    def transcribe_file(
        self,
        file_path: str | Path,
        progress_callback: Callable | None = None,
    ) -> str:
        self.wait_until_ready()
        normalized_path = normalize_media(file_path)
        try:
            return self._transcribe_path(normalized_path, progress_callback=progress_callback)
        finally:
            try:
                os.unlink(normalized_path)
            except OSError:
                pass

    def stream_drafts(
        self,
        frames_provider: Callable[[], list[bytes]],
        stop_event: threading.Event,
        on_draft: Callable[[str], None],
        rate: int = 16000,
    ) -> None:
        """Feed PCM chunks from an in-progress recording into streaming
        inference, emitting draft text after each ~1s of new audio.

        Drafts use local attention with limited context, so they are less
        accurate than the offline pass — callers must replace them with the
        final transcribe_pcm result. Must not run concurrently with other
        inference: the stream switches the shared encoder's attention mode
        until it finishes.
        """
        self.wait_until_ready()
        assert self.model is not None
        min_chunk_bytes = rate * 2  # ~1s of 16-bit mono PCM
        consumed = 0
        try:
            with self.model.transcribe_stream(context_size=(256, 256)) as stream:
                while not stop_event.is_set():
                    frames = frames_provider()
                    available = len(frames)
                    pending = b"".join(frames[consumed:available])
                    if len(pending) < min_chunk_bytes:
                        time.sleep(0.05)
                        continue
                    consumed = available
                    samples = np.frombuffer(pending, dtype=np.int16).astype(np.float32) / 32768.0
                    stream.add_audio(mx.array(samples))
                    text = (stream.result.text or "").strip()
                    if text:
                        on_draft(text)
        finally:
            gc.collect()
            mx.clear_cache()

    def _transcribe_path(
        self,
        file_path: str | Path,
        progress_callback: Callable | None = None,
    ) -> str:
        assert self.model is not None
        # Zero-length audio (e.g. a corrupt or silent media file) crashes the
        # encoder with a Metal allocation error — treat it as "no speech".
        try:
            with wave.open(str(file_path), "rb") as wav_file:
                if wav_file.getnframes() == 0:
                    return ""
        except (wave.Error, OSError):
            pass

        kwargs: dict = {}
        kwargs["chunk_duration"] = 120.0
        kwargs["overlap_duration"] = 15.0
        if progress_callback is not None:
            kwargs["chunk_callback"] = progress_callback
        result = self.model.transcribe(str(file_path), **kwargs)
        text = (getattr(result, "text", "") or "").strip()
        del result
        gc.collect()
        mx.clear_cache()
        return text

class QwenTranscriber:
    """High-accuracy offline transcriber (Qwen3-ASR 1.7B via MLX).

    ~3.4 GB of weights, loaded in the background only while the
    high-accuracy setting is on. No streaming and no mid-inference
    cancellation — used for final passes, with Parakeet as fallback.
    """

    MODEL_ID = "mlx-community/Qwen3-ASR-1.7B-bf16"

    def __init__(self, on_load_failed: Callable[[str], None] | None = None):
        self.model = None
        self.load_error: Exception | None = None
        self._on_load_failed = on_load_failed
        self._load_lock = threading.Lock()
        self._loading = False
        self._discard_when_loaded = False
        self._active_inferences = 0
        self._deferred_close = None

    def start_loading(self) -> None:
        with self._load_lock:
            if self.model is not None:
                return
            if self._loading:
                # Re-enabled while a load is still in flight (off→on toggle):
                # keep the result this time instead of discarding it.
                self._discard_when_loaded = False
                return
            self._loading = True
            self._discard_when_loaded = False
        threading.Thread(target=self._load, daemon=True).start()

    def _load(self) -> None:
        try:
            from qwen3_asr_mlx import Qwen3ASR

            model = Qwen3ASR.from_pretrained(self.MODEL_ID)
            model.warm_up()
            gc.collect()
            mx.clear_cache()

            with self._load_lock:
                discard = self._discard_when_loaded
                self._discard_when_loaded = False
                if not discard:
                    self.model = model
            if discard:
                # The setting was switched off while we were loading.
                model.close()
                gc.collect()
                mx.clear_cache()
            else:
                self.load_error = None
                logger.info("Qwen3-ASR high-accuracy model loaded")
        except Exception as exc:
            self.load_error = exc
            logger.error(f"High-accuracy model failed to load: {exc}")
            if self._on_load_failed is not None:
                self._on_load_failed(str(exc))
        finally:
            with self._load_lock:
                self._loading = False

    def is_ready(self) -> bool:
        return self.model is not None

    def _acquire_model(self):
        """Take an in-use reference so unload() can't close the model out
        from under a running inference."""
        with self._load_lock:
            model = self.model
            if model is None:
                raise TranscriptionError("High-accuracy model not loaded")
            self._active_inferences += 1
            return model

    def _release_model(self) -> None:
        close_target = None
        with self._load_lock:
            self._active_inferences -= 1
            if self._active_inferences == 0 and self._deferred_close is not None:
                close_target = self._deferred_close
                self._deferred_close = None
        if close_target is not None:
            try:
                close_target.close()
            except Exception:
                pass
            gc.collect()
            mx.clear_cache()

    def unload(self) -> None:
        close_target = None
        with self._load_lock:
            if self._loading:
                self._discard_when_loaded = True
            model = self.model
            self.model = None
            if model is not None:
                if self._active_inferences > 0:
                    # An inference is running on this model right now —
                    # closing it would crash mid-Metal-graph. Defer to the
                    # last _release_model().
                    self._deferred_close = model
                else:
                    close_target = model
        if close_target is not None:
            try:
                close_target.close()
            except Exception:
                pass
        gc.collect()
        mx.clear_cache()

    def transcribe_pcm(self, pcm_bytes: bytes, channels: int, sample_width: int, rate: int) -> str:
        if not pcm_bytes:
            return ""
        if sample_width != 2 or rate != 16000:
            raise TranscriptionError("High-accuracy model expects 16-bit 16kHz PCM")

        model = self._acquire_model()
        try:
            samples = np.frombuffer(pcm_bytes, dtype=np.int16).astype(np.float32) / 32768.0
            if channels > 1:
                samples = samples.reshape(-1, channels).mean(axis=1)
            result = model.transcribe(samples, language="en")
            text = (result.text or "").strip()
            gc.collect()
            mx.clear_cache()
            return text
        finally:
            self._release_model()

    def transcribe_file(self, file_path: str | Path) -> str:
        model = self._acquire_model()
        try:
            normalized_path = normalize_media(file_path)
            try:
                try:
                    with wave.open(normalized_path, "rb") as wav_file:
                        if wav_file.getnframes() == 0:
                            return ""
                except (wave.Error, OSError):
                    pass
                result = model.transcribe(normalized_path, language="en")
                text = (result.text or "").strip()
                gc.collect()
                mx.clear_cache()
                return text
            finally:
                try:
                    os.unlink(normalized_path)
                except OSError:
                    pass
        finally:
            self._release_model()


def write_wav_file(frames: bytes, channels: int, sample_width: int, rate: int) -> str:
    with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as temp_file:
        temp_path = temp_file.name

    try:
        with wave.open(temp_path, "wb") as wav_file:
            wav_file.setnchannels(channels)
            wav_file.setsampwidth(sample_width)
            wav_file.setframerate(rate)
            wav_file.writeframes(frames)
    except Exception:
        try:
            os.unlink(temp_path)
        except OSError:
            pass
        raise

    return temp_path


def normalize_media(file_path: str | Path) -> str:
    file_path = Path(file_path)
    if not file_path.exists():
        raise TranscriptionError(f"Media file not found: {file_path}")

    with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as temp_file:
        temp_path = temp_file.name

    ffmpeg_path = resolve_ffmpeg()
    command = [
        ffmpeg_path,
        "-v",
        "error",
        "-y",
        "-i",
        str(file_path),
        "-ac",
        "1",
        "-ar",
        "16000",
        temp_path,
    ]

    try:
        result = subprocess.run(
            command, capture_output=True, text=True, check=False,
            timeout=FFMPEG_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired:
        try:
            os.unlink(temp_path)
        except OSError:
            pass
        raise TranscriptionError(
            f"ffmpeg timed out processing {file_path.name} "
            f"(limit: {FFMPEG_TIMEOUT_SECONDS}s)"
        )

    if result.returncode != 0:
        try:
            os.unlink(temp_path)
        except OSError:
            pass
        stderr = result.stderr.strip() or "ffmpeg failed"
        raise TranscriptionError(f"Could not process {file_path.name}: {stderr}")

    return temp_path


def resolve_ffmpeg() -> str:
    for candidate in FFMPEG_CANDIDATES:
        resolved = shutil.which(candidate) if os.path.sep not in candidate else candidate
        if resolved and Path(resolved).exists():
            return str(Path(resolved))

    raise TranscriptionError(
        "ffmpeg is required for media file transcription. Install it with `brew install ffmpeg`."
    )
