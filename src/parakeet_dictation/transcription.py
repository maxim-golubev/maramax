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
from typing import NamedTuple

import mlx.core as mx
import numpy as np
import pyaudio
from parakeet_mlx import from_pretrained

from .logger_config import setup_logging

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
    def __init__(self):
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
        self._recording_thread: threading.Thread | None = None
        self._stream = None
        self._state_lock = threading.Lock()
        self._stream_lock = threading.Lock()
        # Serializes use of the PyAudio instance (open/enumerate/reinit) so a
        # background device refresh can't tear it down mid-open.
        self._audio_lock = threading.Lock()
        self._cleaned_up = False
        self._selected_device_name: str | None = None

    def set_device(self, name: str | None) -> None:
        self._selected_device_name = name

    def get_selected_device_name(self) -> str | None:
        return self._selected_device_name

    def _reinit_audio(self) -> None:
        try:
            self.audio.terminate()
        except Exception:
            pass
        self.audio = pyaudio.PyAudio()

    def list_input_devices(self) -> list[InputDevice]:
        with self._audio_lock:
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

        self.first_frame_event = threading.Event()
        self.signal_event = threading.Event()

        # Open on the existing audio session — rebuilding it on every start
        # costs ~100ms of lost speech. Rebuild only if the open fails
        # (e.g. the device list changed since init).
        with self._audio_lock:
            try:
                self._open_stream()
            except Exception:
                try:
                    self._reinit_audio()
                    self._open_stream()
                except Exception as exc:
                    logger.error(f"Microphone start failed: {exc}")
                    with self._state_lock:
                        self.recording = False
                        self.last_error = exc
                    self._close_stream()
                    return False

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
                logger.warning("Recording thread did not stop in time, forcing stream close")
                self._close_stream()

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

        self._close_stream()
        with self._audio_lock:
            self.audio.terminate()

    def sample_width(self) -> int:
        return self.audio.get_sample_size(self.format)

    def _open_stream(self) -> None:
        device_index = self._resolve_device_index()

        def callback(in_data, frame_count, time_info, status_flags):
            del frame_count, time_info, status_flags

            if self.is_recording():
                self.frames.append(in_data)
                self.first_frame_event.set()
                if not self.signal_event.is_set() and any(in_data):
                    # A live mic always has a noise floor; exact digital
                    # silence means the route (e.g. a Bluetooth headset
                    # switching into mic mode) isn't delivering audio yet.
                    self.signal_event.set()
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
        if stream is None:
            return

        try:
            while stream.is_active():
                if not self.is_recording():
                    break
                time.sleep(0.01)
        except Exception as exc:
            logger.error(f"Microphone stream error: {exc}")
            with self._state_lock:
                self.last_error = exc
        finally:
            self._close_stream()

    def _close_stream(self) -> None:
        with self._stream_lock:
            stream = self._stream
            self._stream = None

        if stream is None:
            return

        try:
            stream.stop_stream()
        except Exception:
            pass

        try:
            stream.close()
        except Exception:
            pass


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

    def __init__(self):
        self.model = None
        self.load_error: Exception | None = None
        self._load_lock = threading.Lock()
        self._loading = False
        self._discard_when_loaded = False

    def start_loading(self) -> None:
        with self._load_lock:
            if self.model is not None or self._loading:
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
        finally:
            with self._load_lock:
                self._loading = False

    def is_ready(self) -> bool:
        return self.model is not None

    def unload(self) -> None:
        with self._load_lock:
            if self._loading:
                self._discard_when_loaded = True
            model = self.model
            self.model = None
        if model is not None:
            try:
                model.close()
            except Exception:
                pass
        gc.collect()
        mx.clear_cache()

    def transcribe_pcm(self, pcm_bytes: bytes, channels: int, sample_width: int, rate: int) -> str:
        model = self.model
        if model is None:
            raise TranscriptionError("High-accuracy model not loaded")
        if not pcm_bytes:
            return ""
        if sample_width != 2 or rate != 16000:
            raise TranscriptionError("High-accuracy model expects 16-bit 16kHz PCM")

        samples = np.frombuffer(pcm_bytes, dtype=np.int16).astype(np.float32) / 32768.0
        if channels > 1:
            samples = samples.reshape(-1, channels).mean(axis=1)
        result = model.transcribe(samples, language="en")
        text = (result.text or "").strip()
        gc.collect()
        mx.clear_cache()
        return text

    def transcribe_file(self, file_path: str | Path) -> str:
        model = self.model
        if model is None:
            raise TranscriptionError("High-accuracy model not loaded")

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
