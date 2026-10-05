"""Speech recognition: audio in, text out, with the Parakeet or the Qwen engine."""

from __future__ import annotations

import gc
import os
import re
import shutil
import subprocess
import tempfile
import threading
import time
import wave
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from enum import Enum
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
from huggingface_hub import snapshot_download, try_to_load_from_cache
from huggingface_hub.errors import GatedRepoError, RepositoryNotFoundError, RevisionNotFoundError

from .audio_format import FULL_SCALE, SAMPLE_RATE, SAMPLE_WIDTH, whole_samples
from .logger_config import logger
from .paths import RUNTIME_BIN_CANDIDATES
from .written_form import written

FFMPEG_TIMEOUT_SECONDS = 120
CHUNK_SECONDS = 120.0
OVERLAP_SECONDS = 15.0
# How long a draft stream may take to let go of the encoder once told to stop.
DRAFT_RELEASE_SECONDS = 30.0
# What the user is told when a draft stream never let go of the encoder, so
# the standard engine cannot run an offline pass.
ENGINE_STALLED = "Transcription engine stalled — restart the app"
# What the high-accuracy model says before it has been asked to load: only once the standard one is ready.
QWEN_WAITING = "The high-accuracy model loads once the standard model is ready"

# Parakeet sometimes stops formatting partway through a dictation: lower-case
# "i", no capitals, no punctuation, until the window ends. Which stretch it
# happens to depends on exactly where the window starts, so such a stretch is
# recognized again in short windows (which collapsed in none of the archived
# cases) and spliced back in. Measured on archived dictations: docs/validation.md.
COLLAPSE_WORDS_WITH_LOWER_I = 12    # words without punctuation, one of them a lower-case "i"
COLLAPSE_WORDS = 40                 # or this many without punctuation or any capital letter
REPAIR_CHUNK_SECONDS = 20.0
REPAIR_OVERLAP_SECONDS = 4.0
# Audio either side of the stretch, so the words next to it are heard again
# too: the splice cuts at them.
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


class ModelWithdrawn(TranscriptionError):
    """Hugging Face no longer offers the model: no retry can download it."""


@dataclass(frozen=True)
class ModelFiles:
    """A model on Hugging Face, as Maramax loads it."""
    repo: str
    # The commit this release was tested with: what is downloaded when no
    # complete copy is cached, whatever the repository holds by then.
    revision: str
    # What a complete copy holds; one missing any of them (a download cut
    # short) is never loaded.
    files: tuple[str, ...]


PARAKEET = ModelFiles("mlx-community/parakeet-tdt-0.6b-v2", "8ae155301e23d820d82aa60d24817c900e69e487",
                      ("config.json", "model.safetensors"))
QWEN = ModelFiles("mlx-community/Qwen3-ASR-1.7B-bf16", "e1f6c266914abc5a46e8756e02580f834a6cf8a7",
                  ("config.json", "model.safetensors", "vocab.json", "merges.txt"))


def _bare(text: str) -> str:
    """A word or text as said, not as written: no case, no punctuation, no spaces."""
    return re.sub(r"[\W_]+", "", text.casefold())


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
    """How much of the wording two recognitions share (0–1), ignoring case and
    punctuation: twice the longest common subsequence of their words over
    their total, so the order of the arguments does not matter."""
    first, second = [_bare(word.text) for word in original], [_bare(word.text) for word in candidate]
    if not first and not second:
        return 1.0
    shared = [0] * (len(second) + 1)  # One row of the subsequence table.
    for said in first:
        diagonal = 0
        for column, heard in enumerate(second, 1):
            diagonal, shared[column] = shared[column], (diagonal + 1 if said == heard
                                                         else max(shared[column], shared[column - 1]))
    return 2 * shared[-1] / (len(first) + len(second))


def repair_acceptable(stretch: list[Word], replacement: list[Word]) -> bool:
    """Whether `replacement` may stand in for the unformatted `stretch`:
    formatted throughout, and saying the same words."""
    return (bool(replacement) and not collapsed_spans(replacement)
            and word_agreement(stretch, replacement) >= MIN_WORD_AGREEMENT)


def _word_tokens(tokens: list[AlignedToken]) -> list[list[AlignedToken]]:
    """Tokens grouped into words: a token that begins with a space starts one."""
    groups: list[list[AlignedToken]] = []
    for token in tokens:
        if groups and not token.text.startswith(" "):
            groups[-1].append(token)
        else:
            groups.append([token])
    return groups


def _words(tokens: list[AlignedToken]) -> list[Word]:
    return [Word(group[0].start, group[-1].end, "".join(token.text for token in group))
            for group in _word_tokens(tokens)]


def _merge(left: list[AlignedToken], right: list[AlignedToken], overlap_seconds: float) -> list[AlignedToken]:
    """Two overlapping token runs joined on their shared words, as the library does."""
    try:
        return merge_longest_contiguous(left, right, overlap_duration=overlap_seconds)
    except RuntimeError:
        # No run of matching words in the overlap: the looser alignment.
        return merge_longest_common_subsequence(left, right, overlap_duration=overlap_seconds)


def repaired(tokens: list[AlignedToken], candidate: list[AlignedToken], span: tuple[float, float],
             window: tuple[float, float]) -> list[AlignedToken] | None:
    """`tokens` with the unformatted `span` (seconds) replaced by what
    `candidate`, a recognition of `window`, heard there; None when it may
    not be. Only the stretch changes: the cut falls at the words on either
    side of it, which both recognitions must have heard alike (the same
    word, starting within the timing slack), and everything outside is kept
    as it was. Where there is no such word inside the window, the stretch
    begins or ends the capture or a pause longer than the context: the
    candidate is cut at the span's edge instead."""
    said, heard = _words(tokens), _words(candidate)

    def reachable(index: int) -> bool:
        return 0 <= index < len(said) and window[0] <= said[index].start and said[index].end <= window[1]

    def heard_alike(index: int) -> int | None:
        neighbour = said[index]
        alike = [position for position, word in enumerate(heard) if _bare(word.text) == _bare(neighbour.text)
                 and abs(word.start - neighbour.start) <= _TIMING_SLACK_SECONDS]
        return min(alike, key=lambda position: abs(heard[position].start - neighbour.start), default=None)

    def first_from(seconds: float) -> int:
        return next((position for position, word in enumerate(heard) if word.start >= seconds), len(heard))

    inside = [index for index, word in enumerate(said) if span[0] <= word.start and word.end <= span[1]]
    before, after = inside[0] - 1, inside[-1] + 1
    low = heard_alike(before) if reachable(before) else first_from(span[0] - _TIMING_SLACK_SECONDS) - 1
    high = heard_alike(after) if reachable(after) else first_from(span[1] + _TIMING_SLACK_SECONDS)
    if low is None or high is None or not repair_acceptable(said[before + 1:after], heard[low + 1:high]):
        return None
    original, redone = _word_tokens(tokens), _word_tokens(candidate)
    return [token for word in [*original[:before + 1], *redone[low + 1:high], *original[after:]] for token in word]


def model_folder(model: ModelFiles) -> str:
    """A local folder holding the whole of `model`: a complete copy already
    in the Hugging Face cache, used without a single request (the cache's
    latest, or this release's commit); otherwise this release's commit,
    downloaded. Raises ModelWithdrawn when Hugging Face no longer has it."""
    for revision in (None, model.revision):
        folder = _complete_snapshot(model, revision)
        if folder is not None:
            return folder
    try:
        try:
            return snapshot_download(model.repo, revision=model.revision, allow_patterns=list(model.files))
        except RevisionNotFoundError:
            # That commit is gone from a repository that is still there: its
            # latest is the best there is, and loading it says whether it fits.
            logger.warning(f"{model.repo} no longer has {model.revision[:7]}; downloading its latest")
            return snapshot_download(model.repo, allow_patterns=list(model.files))
    except (RepositoryNotFoundError, RevisionNotFoundError, GatedRepoError) as exc:
        raise ModelWithdrawn(f"Hugging Face no longer offers {model.repo}: {exc}") from exc


def _complete_snapshot(model: ModelFiles, revision: str | None) -> str | None:
    """The cached snapshot of `revision` (None: the cache's latest) when it holds every file."""
    try:
        found = [try_to_load_from_cache(model.repo, name, revision=revision) for name in model.files]
    except (OSError, ValueError):
        return None  # An unreadable cache is the same as no cache.
    paths = [Path(path) for path in found if isinstance(path, str)]
    if len(paths) != len(model.files) or not all(path.is_file() for path in paths):
        return None
    folders = {path.parent for path in paths}
    return str(folders.pop()) if len(folders) == 1 else None


def _samples(pcm_bytes: bytes) -> np.ndarray:
    return np.frombuffer(whole_samples(pcm_bytes), dtype=np.int16)


class ParakeetTranscriber:
    """The standard engine. One encoder serves both the offline pass and the
    draft stream, and the stream switches it into a mode in which the offline
    pass produces garbage, so this class owns the stream and refuses an
    offline pass until the stream has let go."""

    def __init__(self, model: ModelFiles = PARAKEET):
        self.model_files = model
        self.model_id = model.repo
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
            self.model = from_pretrained(model_folder(self.model_files))
            self._warm_model()
            logger.info("Parakeet model loaded successfully")
        except Exception as exc:
            self.model = None
            self.load_error = exc
            # The error comes from the model library: its traceback is the diagnosis.
            logger.exception(f"Could not load the Parakeet model {self.model_id}")
            gc.collect()
            mx.clear_cache()
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
        if isinstance(self.load_error, ModelWithdrawn):
            return "Speech model unavailable — Hugging Face no longer offers it"
        if self.load_error is not None:
            return "Speech model unavailable — check your connection, then try again"
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
        with converted_media(file_path) as converted, wave.open(converted, "rb") as audio:
            pcm_bytes = audio.readframes(audio.getnframes())
        return self._transcribe_samples(_samples(pcm_bytes), progress_callback)

    def _require_encoder(self) -> None:
        if not self.finish_drafts():
            raise TranscriptionError(ENGINE_STALLED)

    def _transcribe_samples(self, samples: np.ndarray, progress_callback: Callable | None = None) -> str:
        assert self.model is not None
        config = self.model.preprocessor_config
        # Empty audio crashes the encoder with a Metal allocation error. Under
        # half an FFT (16 ms, nothing anyone said) the library's reflect
        # padding is shorter than _recognize() counts on, so its last frame
        # would read past the buffer.
        if len(samples) <= config.n_fft // 2:
            return ""
        tokens = audio = None
        try:
            audio = mx.array(samples).astype(mx.float32) / FULL_SCALE
            tokens = self._tokens_in_chunks(audio, 0, len(audio), CHUNK_SECONDS, OVERLAP_SECONDS, progress_callback)
            tokens = self._repair_collapses(audio, tokens, progress_callback)
            return written(sentences_to_result(tokens_to_sentences(tokens, DecodingConfig().sentence)).text.strip())
        finally:
            # Cancellation and inference errors need cleanup too, otherwise
            # repeated failed sessions can retain Metal's cached allocations.
            # The audio goes too: once this returns, its buffer would wait in
            # the cache until the next inference.
            del tokens, audio
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
                spliced = repaired(tokens, candidate, (span_start, span_end), (first / rate, last / rate))
                if spliced is not None:
                    tokens = spliced
                    logger.info(f"Recognized an unformatted stretch again ({span_start:.0f}–{span_end:.0f} s)")
                    break
        return tokens

    # -- Draft stream --

    def start_drafts(self, frames_provider: Callable[[], list[bytes]], on_draft: Callable[[str], None]) -> bool:
        """Emit draft text while a recording is in progress, about once per
        two seconds of new audio. Drafts use limited context and are less accurate
        than the offline pass, which must replace them. False when an earlier
        stream never let go of the encoder: a second one must not start."""
        if self._drafts is not None and self._drafts.is_alive():
            logger.warning("Previous draft stream still running; no drafts for this recording")
            return False
        # A stream found wedged has since let go: the new one has not been
        # waited for yet, so finish_drafts() must give it the full wait.
        self._drafts_wedged = False
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
            # Each step encodes the stream's whole context window (about 20 s)
            # again: at 2 s steps the GPU is busy a fifth of the time, not two
            # fifths, and the drafts agree better with the final pass.
            min_chunk_bytes = 2 * SAMPLE_RATE * SAMPLE_WIDTH
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
        except Exception:
            # Drafts are a convenience; the offline pass still runs.
            logger.warning("Live preview unavailable: the draft stream failed", exc_info=True)
        finally:
            gc.collect()
            mx.clear_cache()


class Echo(Enum):
    """Whether the high-accuracy model's answer is its context said back,
    which it tends to answer with on audio that has no speech in it."""
    NONE = "none"           # a transcript
    POSSIBLE = "possible"   # only vocabulary terms: said by the user, or the term list repeated
    CERTAIN = "certain"     # the context with its label ("Vocabulary: …"): not speech


def context_echo(text: str, context: str) -> Echo:
    """How far `text` could be `context` repeated: the beginning of the
    context with its label, or all of it and more, is an echo; the
    beginning of its term list could be one. Speech that only starts with
    the terms is a transcript."""
    heard = _bare(text)
    labelled, terms = _bare(context), _bare(context.partition(":")[2])
    if heard and terms.startswith(heard):
        return Echo.POSSIBLE
    if heard and (labelled.startswith(heard) or heard.startswith(labelled)):
        return Echo.CERTAIN
    return Echo.NONE


class QwenTranscriber:
    """High-accuracy offline transcriber (Qwen3-ASR 1.7B via MLX).

    ~4.1 GB of weights, loaded in the background only while the
    high-accuracy setting is on. No streaming and no mid-inference
    cancellation — used for final passes, with Parakeet as fallback.
    """

    MODEL_ID = QWEN.repo

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
        # The load ends (_loading cleared) in the same locked step that
        # decides what becomes of the model, before any slow cleanup: a
        # start_loading() during that cleanup then starts a load of its own
        # instead of being taken for this one and lost.
        model = None
        try:
            from qwen3_asr_mlx import Qwen3ASR

            model = Qwen3ASR.from_pretrained(model_folder(QWEN))
            model.warm_up()
            gc.collect()
            mx.clear_cache()
        except Exception as exc:
            with self._load_lock:
                self._loading = False
                self.load_error = exc
            # The error comes from the model library: its traceback is the diagnosis.
            logger.exception(f"Could not load the high-accuracy model {self.MODEL_ID}")
            if model is not None:
                self._close(model)
            gc.collect()
            mx.clear_cache()
            if self._on_load_failed is not None:
                self._on_load_failed(str(exc))
            return

        with self._load_lock:
            self._loading = False
            keep = not self._discard_when_loaded
            self._discard_when_loaded = False
            if keep:
                self.model = model
                self.load_error = None
        if not keep:
            # The setting was switched off while the model was loading.
            self._close(model)
            gc.collect()
            mx.clear_cache()
            return
        logger.info("Qwen3-ASR high-accuracy model loaded")
        if self._on_loaded is not None:
            self._on_loaded()

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

    def status_message(self) -> str:
        if self.is_ready():
            return "High-accuracy model ready"
        if self.failed:
            return "High-accuracy model could not be loaded — using the standard model"
        if not self._loading:
            return QWEN_WAITING
        return "Loading the high-accuracy model…"

    @property
    def failed(self) -> bool:
        """The last load failed and no other is under way: it can be tried again."""
        return self.load_error is not None and not self._loading

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
    def _transcript(model, audio: np.ndarray | str, context: str | None) -> str:
        """What `model` hears in `audio` (samples or a WAV file), told the
        spellings in `context`. An answer that is certainly the context said
        back is no text, so the caller's fallback decides. One made only of
        vocabulary terms is returned: context_echo() tells the caller it may
        be either, and only the audio can tell which (without its context
        the model answers noise with words of its own)."""
        result = model.transcribe(audio, language="en", context=context)
        text = (result.text or "").strip()
        del result
        return "" if context and context_echo(text, context) is Echo.CERTAIN else text

    def transcribe_pcm(self, pcm_bytes: bytes, context: str | None = None) -> str:
        """Recognize a capture in the app's PCM format. `context` is free
        text the model reads before listening, used to pass the spellings of
        names and jargon the user cares about."""
        if not pcm_bytes:
            return ""

        model = self._acquire_model()
        samples = None
        try:
            samples = _samples(pcm_bytes).astype(np.float32) / FULL_SCALE
            return self._transcript(model, samples, context)
        finally:
            del samples
            self._release_model()
            gc.collect()
            mx.clear_cache()

    def transcribe_file(self, file_path: str | Path, context: str | None = None) -> str:
        model = self._acquire_model()
        try:
            with converted_media(file_path) as converted:
                try:
                    with wave.open(converted, "rb") as wav_file:
                        if wav_file.getnframes() == 0:
                            return ""
                except (wave.Error, OSError):
                    pass  # Not inspectable as WAV: let the model report what it finds.
                return self._transcript(model, converted, context)
        finally:
            self._release_model()
            gc.collect()
            mx.clear_cache()


@contextmanager
def converted_media(file_path: str | Path) -> Iterator[str]:
    """`file_path` converted by FFmpeg to the app's PCM format, as a
    temporary WAV file that exists only inside the `with`."""
    file_path = Path(file_path)
    if not file_path.exists():
        raise TranscriptionError(f"Media file not found: {file_path}")

    ffmpeg_path = resolve_ffmpeg()  # Before the temporary file: it raises when FFmpeg is absent.
    with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as temp_file:
        temp_path = temp_file.name
    command = [ffmpeg_path, "-v", "error", "-y", "-i", str(file_path),
               "-ac", "1", "-ar", str(SAMPLE_RATE), "-sample_fmt", "s16", temp_path]
    try:
        try:
            result = subprocess.run(command, capture_output=True, text=True, check=False,
                                    timeout=FFMPEG_TIMEOUT_SECONDS)
        except subprocess.TimeoutExpired as exc:
            raise TranscriptionError(
                f"FFmpeg took over {FFMPEG_TIMEOUT_SECONDS} s to convert the file") from exc
        except OSError as exc:
            logger.error(f"FFmpeg at {ffmpeg_path} did not start for {file_path}: {exc}")
            raise TranscriptionError("FFmpeg did not start") from exc
        if result.returncode != 0:
            logger.error(f"FFmpeg could not convert {file_path} (exit {result.returncode}): {result.stderr.strip()}")
            raise TranscriptionError("FFmpeg could not read the file")
        yield temp_path
    finally:
        try:
            Path(temp_path).unlink(missing_ok=True)
        except OSError as exc:
            # The transcript, or the error that brought us here, matters more
            # than a stray copy in the temporary folder: say where it is.
            logger.warning(f"Could not delete the converted copy of {file_path.name} at {temp_path}: {exc}")


def resolve_ffmpeg() -> str:
    # The same directories the app adds to PATH at start-up, searched here as
    # well so a conversion works even where PATH was never extended.
    search = os.pathsep.join([os.environ.get("PATH", ""), *(c for c in RUNTIME_BIN_CANDIDATES if os.path.isabs(c))])
    found = shutil.which("ffmpeg", path=search)
    if found is None:
        raise TranscriptionError("Importing media needs FFmpeg — install it with brew install ffmpeg")
    return found
