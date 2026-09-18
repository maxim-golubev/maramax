"""Parakeet and Qwen speech recognizers on MLX, plus WAV and FFmpeg helpers."""

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

import mlx.core as mx
import numpy as np
from parakeet_mlx import from_pretrained
from huggingface_hub import try_to_load_from_cache

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


def cached_model_source(model_id: str) -> str:
    """Use a complete existing snapshot without any HTTP freshness checks."""
    if Path(model_id).is_dir():
        return model_id
    try:
        config = try_to_load_from_cache(model_id, "config.json")
        weights = try_to_load_from_cache(model_id, "model.safetensors")
        if isinstance(config, str) and isinstance(weights, str):
            config_path, weights_path = Path(config), Path(weights)
            if config_path.is_file() and weights_path.is_file() and config_path.parent == weights_path.parent:
                return str(config_path.parent)
    except (OSError, ValueError):
        pass
    return model_id


class ParakeetTranscriber:
    def __init__(self, model_id: str = "mlx-community/parakeet-tdt-0.6b-v2"):
        self.model_id = model_id
        self.model = None
        self.load_error: Exception | None = None
        self.ready_event = threading.Event()
        self._load_lock = threading.Lock()
        self._loader = threading.Thread(target=self._load_model, daemon=True)
        self._loader.start()

    def retry_loading(self) -> bool:
        with self._load_lock:
            if not self.ready_event.is_set() or self.load_error is None:
                return False
            self.load_error = None
            self.model = None
            self.ready_event.clear()
            self._loader = threading.Thread(target=self._load_model, daemon=True)
            self._loader.start()
            return True

    def _load_model(self) -> None:
        try:
            self.model = from_pretrained(cached_model_source(self.model_id))
            self._warm_model()
            logger.info("Parakeet model loaded successfully")
        except Exception as exc:
            self.model = None
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
        finally:
            gc.collect()
            mx.clear_cache()
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
        result = None
        try:
            result = self.model.transcribe(str(file_path), **kwargs)
            return (getattr(result, "text", "") or "").strip()
        finally:
            # Cancellation and inference errors need cleanup too, otherwise
            # repeated failed sessions can retain Metal's cached allocations.
            del result
            gc.collect()
            mx.clear_cache()

class QwenTranscriber:
    """High-accuracy offline transcriber (Qwen3-ASR 1.7B via MLX).

    ~4.1 GB of weights, loaded in the background only while the
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
        model = None
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
            if model is not None:
                try:
                    model.close()
                except Exception:
                    pass
            gc.collect()
            mx.clear_cache()
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
        result = None
        samples = None
        try:
            samples = np.frombuffer(pcm_bytes, dtype=np.int16).astype(np.float32) / 32768.0
            if channels > 1:
                samples = samples.reshape(-1, channels).mean(axis=1)
            result = model.transcribe(samples, language="en")
            text = (result.text or "").strip()
            return text
        finally:
            del result, samples
            self._release_model()
            gc.collect()
            mx.clear_cache()

    def transcribe_file(self, file_path: str | Path) -> str:
        model = self._acquire_model()
        result = None
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
                return text
            finally:
                try:
                    os.unlink(normalized_path)
                except OSError:
                    pass
        finally:
            del result
            self._release_model()
            gc.collect()
            mx.clear_cache()


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
    except OSError as exc:
        Path(temp_path).unlink(missing_ok=True)
        raise TranscriptionError(f"Could not start media conversion: {exc}") from exc

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
