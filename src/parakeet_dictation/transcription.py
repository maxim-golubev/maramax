"""Speech recognition: audio in, text out, with the Parakeet or the Qwen engine."""

from __future__ import annotations

import difflib
import gc
import os
import re
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
from parakeet_mlx import from_pretrained
from parakeet_mlx.alignment import (
    AlignedToken,
    merge_longest_common_subsequence,
    merge_longest_contiguous,
    sentences_to_result,
    tokens_to_sentences,
)
from parakeet_mlx.audio import get_logmel
from parakeet_mlx.parakeet import DecodingConfig
from huggingface_hub import try_to_load_from_cache

from .audio_format import FULL_SCALE, SAMPLE_RATE, SAMPLE_WIDTH, whole_samples
from .logger_config import logger
from .paths import RUNTIME_BIN_CANDIDATES

FFMPEG_TIMEOUT_SECONDS = 120
CHUNK_SECONDS = 120.0
OVERLAP_SECONDS = 15.0
# How long a draft stream may take to let go of the encoder once told to stop.
DRAFT_RELEASE_SECONDS = 30.0

# Parakeet sometimes stops formatting partway through a dictation: lower-case
# "i", no capitals, no punctuation, until the window ends. Which stretch it
# happens to depends on exactly where the window starts, so such a stretch is
# recognized again in short windows (which collapsed in none of the archived
# cases) and spliced back in. Measured on archived dictations: docs/validation.md.
COLLAPSE_WORDS_WITH_LOWER_I = 12    # words without punctuation, one of them a lower-case "i"
COLLAPSE_WORDS = 40                 # or this many without punctuation or any capital letter
REPAIR_CHUNK_SECONDS = 20.0
REPAIR_OVERLAP_SECONDS = 4.0
# Audio either side of the stretch. It must exceed the overlap, so the seams
# fall in formatted text, outside the stretch.
REPAIR_CONTEXT_SECONDS = 5.0
REPAIR_RETRY_LEAD_SECONDS = 15.0    # a window that collapsed too is tried once more, starting this early
# A repair may change capitals and punctuation, not what was said.
MIN_WORD_AGREEMENT = 0.9
MAX_STRETCHES = 6                   # unformatted stretches tried per capture
_PUNCTUATION = re.compile(r"[.,?!;:]")
_LOWER_I = re.compile(r"i(?:['’][a-z]+)?")
_CAPITAL = re.compile(r"[A-Z]")
_TIMING_SLACK_SECONDS = 0.5


class TranscriptionError(RuntimeError):
    pass


class TranscriptionCancelled(TranscriptionError):
    """The user cancelled: raised from a progress callback to stop recognition."""


class Word(NamedTuple):
    start: float  # seconds
    end: float
    text: str


def collapsed_spans(words: list[Word]) -> list[tuple[float, float]]:
    """Where the recognizer stopped formatting, as (start, end) seconds: a run
    of words without punctuation that writes "I" in lower case, which
    formatted output never does, or a long one with no capital letter at all.
    A run-on sentence that is still formatted keeps its capital I and passes."""
    spans = []
    run: list[Word] = []
    for word in [*words, None]:
        if word is not None and not _PUNCTUATION.search(word.text):
            run.append(word)
            continue
        lower_i = any(_LOWER_I.fullmatch(w.text.strip()) for w in run)
        uncapitalized = not any(_CAPITAL.search(w.text) for w in run)
        if (lower_i and len(run) >= COLLAPSE_WORDS_WITH_LOWER_I) or (uncapitalized and len(run) >= COLLAPSE_WORDS):
            spans.append((run[0].start, run[-1].end))
        run = []
    return spans


def word_agreement(original: list[Word], candidate: list[Word]) -> float:
    """How much of the wording two recognitions share (0–1), ignoring case and punctuation."""
    def bare(words: list[Word]) -> list[str]:
        return [re.sub(r"[\W_]+", "", word.text.casefold()) for word in words]
    return difflib.SequenceMatcher(a=bare(original), b=bare(candidate), autojunk=False).ratio()


def replaced_range(start: float, end: float, seconds: float) -> tuple[float, float]:
    """The part of a repair window from `start` to `end` seconds that a
    splice takes from the re-recognition alone. Within one overlap of an
    inner edge the two are merged on the words they share; an edge at the
    start or end of the audio has nothing to merge with."""
    return (start + REPAIR_OVERLAP_SECONDS if start > 0 else 0.0,
            end - REPAIR_OVERLAP_SECONDS if end < seconds else seconds)


def repair_acceptable(original: list[Word], candidate: list[Word], span: tuple[float, float],
                      replaced: tuple[float, float]) -> bool:
    """Whether `candidate` may replace what lies in `replaced`: formatted
    throughout, and saying the same words both in the unformatted `span` and
    in the formatted text around it, judged apart so agreeing context cannot
    hide a change inside the stretch. Word times move a little between
    recognitions, so each comparison reaches slightly past its ends."""
    def within(words: list[Word], low: float, high: float) -> list[Word]:
        return [word for word in words if low - _TIMING_SLACK_SECONDS <= word.start < high + _TIMING_SLACK_SECONDS]

    def around_span(words: list[Word]) -> list[Word]:
        inside = set(map(id, within(words, *span)))
        return [word for word in within(words, *replaced) if id(word) not in inside]

    return (bool(candidate) and not collapsed_spans(candidate)
            and word_agreement(within(original, *span), within(candidate, *span)) >= MIN_WORD_AGREEMENT
            and word_agreement(around_span(original), around_span(candidate)) >= MIN_WORD_AGREEMENT)


def _words(tokens: list[AlignedToken]) -> list[Word]:
    """Tokens joined into words: a token that begins with a space starts one."""
    words: list[Word] = []
    for token in tokens:
        if words and not token.text.startswith(" "):
            last = words[-1]
            words[-1] = Word(last.start, token.end, last.text + token.text)
        else:
            words.append(Word(token.start, token.end, token.text))
    return words


def _merge(left: list[AlignedToken], right: list[AlignedToken], overlap_seconds: float) -> list[AlignedToken]:
    """Two overlapping token runs joined on their shared words, as the library does."""
    try:
        return merge_longest_contiguous(left, right, overlap_duration=overlap_seconds)
    except RuntimeError:
        # No run of matching words in the overlap: the looser alignment.
        return merge_longest_common_subsequence(left, right, overlap_duration=overlap_seconds)


def _splice(tokens: list[AlignedToken], replacement: list[AlignedToken], start: float, end: float,
            seconds: float) -> list[AlignedToken]:
    """`tokens` with what lies between `start` and `end` seconds taken from
    `replacement`, joined on the words both have near each edge. An edge at
    the start or end of the audio has nothing beyond it to join to: the
    replacement runs to it."""
    low, high = replaced_range(start, end, seconds)
    before = [token for token in tokens if token.start < low] if start > 0 else []
    after = [token for token in tokens if token.end > high] if end < seconds else []
    joined = _merge(before, replacement, REPAIR_OVERLAP_SECONDS) if before else replacement
    return _merge(joined, after, REPAIR_OVERLAP_SECONDS) if after else joined


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
        pass  # An unreadable cache is the same as no cache: load by id.
    return model_id


def _samples(pcm_bytes: bytes) -> np.ndarray:
    return np.frombuffer(whole_samples(pcm_bytes), dtype=np.int16)


class ParakeetTranscriber:
    """The standard engine. One encoder serves both the offline pass and the
    draft stream, and the stream switches it into a mode in which the offline
    pass produces garbage, so this class owns the stream and refuses an
    offline pass until the stream has let go."""

    def __init__(self, model_id: str = "mlx-community/parakeet-tdt-0.6b-v2"):
        self.model_id = model_id
        self.model = None
        self.load_error: Exception | None = None
        self.ready_event = threading.Event()
        self._load_lock = threading.Lock()
        self._drafts: threading.Thread | None = None
        self._drafts_stop = threading.Event()
        self._drafts_wedged = False
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
        self._transcribe_samples(np.zeros(int(0.3 * SAMPLE_RATE), dtype=np.int16))

    def wait_until_ready(self) -> None:
        self.ready_event.wait()
        if self.load_error is not None:
            raise TranscriptionError(f"Model failed to load: {self.load_error}") from self.load_error
        if self.model is None:
            raise TranscriptionError("Model failed to initialize")

    def is_ready(self) -> bool:
        return self.ready_event.is_set() and self.model is not None and self.load_error is None

    def status_message(self) -> str:
        if self.is_ready():
            return "Speech model ready"
        if self.load_error is not None:
            return "Speech model unavailable — check your connection, then retry"
        return "Preparing the speech model — the first launch downloads it"

    # -- Offline pass --

    def transcribe_pcm(self, pcm_bytes: bytes, progress_callback: Callable | None = None) -> str:
        """Recognize a capture in the app's PCM format, entirely in memory."""
        if not pcm_bytes:
            return ""
        self.wait_until_ready()
        self._require_encoder()
        return self._transcribe_samples(_samples(pcm_bytes), progress_callback)

    def transcribe_file(
        self,
        file_path: str | Path,
        progress_callback: Callable | None = None,
    ) -> str:
        self.wait_until_ready()
        self._require_encoder()
        normalized_path = normalize_media(file_path)
        try:
            with wave.open(normalized_path, "rb") as audio:
                pcm_bytes = audio.readframes(audio.getnframes())
        finally:
            Path(normalized_path).unlink(missing_ok=True)
        return self._transcribe_samples(_samples(pcm_bytes), progress_callback)

    def _require_encoder(self) -> None:
        if not self.finish_drafts():
            raise TranscriptionError("Transcription engine stalled — restart the app")

    def _transcribe_samples(self, samples: np.ndarray, progress_callback: Callable | None = None) -> str:
        assert self.model is not None
        config = self.model.preprocessor_config
        # Empty audio crashes the encoder with a Metal allocation error, and
        # less than one analysis hop cannot form a spectrogram frame.
        if len(samples) < config.hop_length:
            return ""
        tokens = None
        try:
            audio = mx.array(samples).astype(mx.float32) / FULL_SCALE
            tokens = self._tokens_in_chunks(audio, 0, len(audio), CHUNK_SECONDS, OVERLAP_SECONDS, progress_callback)
            tokens = self._repair_collapses(audio, tokens, progress_callback)
            return sentences_to_result(tokens_to_sentences(tokens, DecodingConfig().sentence)).text.strip()
        finally:
            # Cancellation and inference errors need cleanup too, otherwise
            # repeated failed sessions can retain Metal's cached allocations.
            del tokens
            gc.collect()
            mx.clear_cache()

    def _recognize(self, audio: mx.array):
        assert self.model is not None
        config = self.model.preprocessor_config
        # The library's spectrogram sizes its frames by the FFT length but
        # counts them by the shorter window, so the last frame can read up
        # to 112 samples past the end of the buffer. Those samples are
        # multiplied by zero, which is harmless only while that memory holds
        # finite numbers. Zeros appended up to the last frame's end keep the
        # read inside the buffer without adding a frame.
        frames = (len(audio) + config.n_fft - config.win_length + config.hop_length) // config.hop_length
        overread = (frames - 1) * config.hop_length - len(audio)
        if overread > 0:
            audio = mx.pad(audio, [(0, overread)])
        return self.model.generate(get_logmel(audio, config))[0]

    def _tokens_in_chunks(self, audio: mx.array, first: int, last: int, chunk_seconds: float,
                          overlap_seconds: float, progress_callback: Callable | None) -> list[AlignedToken]:
        """The tokens of audio[first:last] (sample indices), timed from the
        start of `audio`. Audio longer than a chunk is cut into overlapping
        chunks merged on their shared words: the procedure of parakeet_mlx's
        own transcribe() (0.5.x), run on samples already in memory so a long
        dictation needs neither a file nor FFmpeg. Unlike the library's loop
        it stops at the chunk that reaches the end; one more would lie wholly
        inside that chunk's overlap, and merging it can repeat words."""
        assert self.model is not None
        config = self.model.preprocessor_config
        chunk = int(chunk_seconds * config.sample_rate)
        step = chunk if last - first <= chunk else chunk - int(overlap_seconds * config.sample_rate)
        tokens: list[AlignedToken] = []
        for piece_start in range(first, last, step):
            piece_end = min(piece_start + chunk, last)
            if progress_callback is not None:
                progress_callback(piece_end, len(audio))
            if piece_end - piece_start < config.hop_length:
                break
            piece = self._recognize(audio[piece_start:piece_end])
            offset = piece_start / config.sample_rate
            for sentence in piece.sentences:
                for token in sentence.tokens:
                    token.start += offset
                    token.end = token.start + token.duration
            tokens = _merge(tokens, piece.tokens, overlap_seconds) if tokens else piece.tokens
            if piece_end == last:
                break
        return tokens

    def _repair_collapses(self, audio: mx.array, tokens: list[AlignedToken],
                          progress_callback: Callable | None) -> list[AlignedToken]:
        """Recognize each stretch the model left unformatted again, in short
        windows, and splice it in when that comes out formatted with the same
        words. A window that fails is tried once more starting earlier;
        failing that, the stretch stays as it was. A cancel ends the repair
        and keeps what is recognized: the first pass is a whole transcript."""
        assert self.model is not None
        rate = self.model.preprocessor_config.sample_rate
        seconds = len(audio) / rate
        # Recognition is deterministic, so a window is never recognized
        # twice; a short capture's first pass already was its only window.
        tried = {(0, len(audio))} if len(audio) <= int(REPAIR_CHUNK_SECONDS * rate) else set()
        given_up: set[tuple[float, float]] = set()
        for _ in range(MAX_STRETCHES):
            pending = [span for span in collapsed_spans(_words(tokens)) if span not in given_up]
            if not pending:
                break
            span_start, span_end = pending[0]
            given_up.add((span_start, span_end))  # Replaced by new spans if a splice succeeds.
            for lead in (REPAIR_CONTEXT_SECONDS, REPAIR_RETRY_LEAD_SECONDS):
                first = int(max(0.0, span_start - lead) * rate)
                last = min(len(audio), int((span_end + REPAIR_CONTEXT_SECONDS) * rate))
                if (first, last) in tried:
                    continue
                tried.add((first, last))
                if progress_callback is not None:
                    try:
                        progress_callback(len(audio), len(audio))
                    except TranscriptionCancelled:
                        logger.info("Cancelled while re-recognizing an unformatted stretch; keeping the first pass")
                        return tokens
                candidate = self._tokens_in_chunks(audio, first, last, REPAIR_CHUNK_SECONDS, REPAIR_OVERLAP_SECONDS,
                                                   None)
                if repair_acceptable(_words(tokens), _words(candidate), (span_start, span_end),
                                     replaced_range(first / rate, last / rate, seconds)):
                    tokens = _splice(tokens, candidate, first / rate, last / rate, seconds)
                    logger.info(f"Recognized an unformatted stretch again ({span_start:.0f}–{span_end:.0f} s)")
                    break
        return tokens

    # -- Draft stream --

    def start_drafts(self, frames_provider: Callable[[], list[bytes]], on_draft: Callable[[str], None]) -> bool:
        """Emit draft text while a recording is in progress, about once per
        second of new audio. Drafts use limited context and are less accurate
        than the offline pass, which must replace them. False when an earlier
        stream never let go of the encoder: a second one must not start."""
        if self._drafts is not None and self._drafts.is_alive():
            logger.warning("Previous draft stream still running; no drafts for this recording")
            return False
        stop = self._drafts_stop = threading.Event()
        self._drafts = threading.Thread(target=self._stream_drafts, args=(frames_provider, stop, on_draft), daemon=True)
        self._drafts.start()
        return True

    def finish_drafts(self) -> bool:
        """Stop the draft stream. True once the encoder is free for the
        offline pass; False if the stream is wedged and still holds it."""
        thread = self._drafts
        if thread is None:
            return True
        self._drafts_stop.set()
        # The full wait is paid once; a stream already known to be wedged is
        # only looked at again, not waited for (a queue asks once per file).
        thread.join(timeout=0 if self._drafts_wedged else DRAFT_RELEASE_SECONDS)
        if thread.is_alive():
            if not self._drafts_wedged:
                logger.error("Draft stream wedged; the standard engine cannot run an offline pass")
            self._drafts_wedged = True
            return False
        self._drafts = None
        self._drafts_wedged = False
        return True

    def _stream_drafts(self, frames_provider: Callable[[], list[bytes]], stop: threading.Event,
                       on_draft: Callable[[str], None]) -> None:
        try:
            self.wait_until_ready()
            assert self.model is not None
            min_chunk_bytes = SAMPLE_RATE * SAMPLE_WIDTH  # ~1 s
            consumed = 0
            with self.model.transcribe_stream(context_size=(256, 256)) as stream:
                while not stop.is_set():
                    frames = frames_provider()
                    available = len(frames)
                    pending = b"".join(frames[consumed:available])
                    if len(pending) < min_chunk_bytes:
                        time.sleep(0.05)
                        continue
                    consumed = available
                    stream.add_audio(mx.array(_samples(pending).astype(np.float32) / FULL_SCALE))
                    text = (stream.result.text or "").strip()
                    if text:
                        on_draft(text)
        except Exception as exc:
            # Drafts are a convenience; the offline pass still runs.
            logger.warning(f"Live preview unavailable: {exc}")
        finally:
            gc.collect()
            mx.clear_cache()


class QwenTranscriber:
    """High-accuracy offline transcriber (Qwen3-ASR 1.7B via MLX).

    ~4.1 GB of weights, loaded in the background only while the
    high-accuracy setting is on. No streaming and no mid-inference
    cancellation — used for final passes, with Parakeet as fallback.
    """

    MODEL_ID = "mlx-community/Qwen3-ASR-1.7B-bf16"

    def __init__(self, on_load_failed: Callable[[str], None] | None = None,
                 on_loaded: Callable[[], None] | None = None):
        self.model = None
        self.load_error: Exception | None = None
        self._on_load_failed = on_load_failed
        self._on_loaded = on_loaded
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

            model = Qwen3ASR.from_pretrained(cached_model_source(self.MODEL_ID))
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
                if self._on_loaded is not None:
                    self._on_loaded()
        except Exception as exc:
            if model is not None:
                self._close(model)
            gc.collect()
            mx.clear_cache()
            self.load_error = exc
            logger.error(f"High-accuracy model failed to load: {exc}")
            if self._on_load_failed is not None:
                self._on_load_failed(str(exc))
        finally:
            with self._load_lock:
                self._loading = False

    @staticmethod
    def _close(model) -> None:
        try:
            model.close()
        except Exception as exc:
            # The weights are reclaimed by the collector either way; a failed
            # close must not stop the caller from releasing the rest.
            logger.warning(f"High-accuracy model did not close cleanly: {exc}")

    def is_ready(self) -> bool:
        return self.model is not None

    def is_loading(self) -> bool:
        return self._loading

    def status_message(self) -> str:
        if self.is_ready():
            return "High-accuracy model ready"
        if self.load_error is not None and not self._loading:
            return "High-accuracy model could not be loaded — using the standard model"
        return "Loading the high-accuracy model…"

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
            self._close(close_target)
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
            self._close(close_target)
        gc.collect()
        mx.clear_cache()

    @staticmethod
    def _without_echo(text: str, context: str | None) -> str:
        """Given audio with no speech in it, the model tends to answer with
        the context it was handed. That is not a transcript: report no text
        so the caller's fallback decides."""
        if not context or not text:
            return text

        def squash(value: str) -> str:
            return re.sub(r"[\W_]+", "", value.casefold())

        heard = squash(text)
        # The whole context, or just what follows its label ("Vocabulary: …").
        echoes = [squash(context), squash(context.partition(":")[2])]
        return "" if heard and any(e and (e.startswith(heard) or heard.startswith(e)) for e in echoes) else text

    def transcribe_pcm(self, pcm_bytes: bytes, context: str | None = None) -> str:
        """Recognize a capture in the app's PCM format. `context` is free
        text the model reads before listening, used to pass the spellings of
        names and jargon the user cares about."""
        if not pcm_bytes:
            return ""

        model = self._acquire_model()
        result = None
        samples = None
        try:
            samples = _samples(pcm_bytes).astype(np.float32) / FULL_SCALE
            result = model.transcribe(samples, language="en", context=context)
            return self._without_echo((result.text or "").strip(), context)
        finally:
            del result, samples
            self._release_model()
            gc.collect()
            mx.clear_cache()

    def transcribe_file(self, file_path: str | Path, context: str | None = None) -> str:
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
                    pass  # Not inspectable as WAV: let the model report what it finds.
                result = model.transcribe(normalized_path, language="en", context=context)
                return self._without_echo((result.text or "").strip(), context)
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


def normalize_media(file_path: str | Path) -> str:
    file_path = Path(file_path)
    if not file_path.exists():
        raise TranscriptionError(f"Media file not found: {file_path}")

    ffmpeg_path = resolve_ffmpeg()  # Before the temporary file: it raises when FFmpeg is absent.
    with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as temp_file:
        temp_path = temp_file.name

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
        str(SAMPLE_RATE),
        "-sample_fmt",
        "s16",
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
    # The same directories the app adds to PATH at start-up, searched here as
    # well so a conversion works even where PATH was never extended.
    search = os.pathsep.join([os.environ.get("PATH", ""), *(c for c in RUNTIME_BIN_CANDIDATES if os.path.isabs(c))])
    found = shutil.which("ffmpeg", path=search)
    if found is None:
        raise TranscriptionError(
            "ffmpeg is required for media file transcription. Install it with `brew install ffmpeg`."
        )
    return found
