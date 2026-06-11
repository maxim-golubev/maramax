# Maramax

On-device speech-to-text macOS menu bar app. Transcribes via global hotkey (Option+Space), never sends audio off-machine. Built on NVIDIA Parakeet ASR running locally through MLX on Apple Silicon.

## Quick Start

```bash
brew install portaudio ffmpeg
uv venv -p 3.12 && uv sync
./run.sh
```

Build standalone .app:
```bash
uv sync --extra dev
bash build_app.sh
cp -R dist/Maramax.app /Applications/
```

First launch downloads the Parakeet model (~400 MB). After that, starts in seconds.

## Running Tests

```bash
pytest                # all tests
pytest tests/ -v      # verbose
```

Tests use `tmp_path`, monkeypatching, and no heavy mocking (no PyAudio/PyObjC/model mocks). Coverage focuses on utilities: paths, clipboard, hotkey encoding, history store persistence/migration, queue operations, and export logic.

Linting/type checking (dev extras required):
```bash
ruff check src/ tests/
mypy src/
```

## Architecture

Python menu bar app (`rumps`) with a native AppKit overlay (`PyObjC`). Audio capture via `PyAudio`, global hotkeys via Carbon API (ctypes), transcription via `parakeet-mlx` on MLX.

### Module Map

```
src/parakeet_dictation/
  main.py            Entry point. Parses --version, inits DictationApp, installs signal handlers.
  app.py             Core controller (DictationApp). Owns all state, coordinates components, Settings menu.
  overlay.py         Native NSPanel overlay: drop zone, device selector, text view, queue tab, controls.
  transcription.py   AudioRecorder (PyAudio callback streaming) + ParakeetTranscriber (model loading, chunked inference, streaming drafts) + QwenTranscriber (high-accuracy final passes) + shared FFmpeg/WAV helpers.
  queue.py           TranscriptionQueue (thread-safe item list), QueueItem dataclass, OutputMode/OutputConfig for save options.
  export.py          export_results() writes completed queue items to clipboard, individual files, or single file.
  hotkeys.py         GlobalHotKeyManager. Registers Option+Space via Carbon API ctypes bindings.
  autopaste.py       Synthetic Cmd+V via CoreGraphics CGEvent ctypes bindings + Accessibility trust check.
  history.py         HistoryStore. Thread-safe JSON persistence in ~/Library/Application Support/Maramax/. Auto-migrates legacy ParakeetDictation data.
  clipboard.py       copy_text() wrapper around pyperclip with ClipboardError.
  config.py          AppConfig (mutable, persisted to settings.json with type-validated load) and frozen ShortcutConfig.
  paths.py           resource_path() resolves assets in dev vs bundle. app_support_dir(). ensure_runtime_path() prepends homebrew/bundle bins to PATH.
  logger_config.py   Colored console logging. Reads LOG_LEVEL env var, supports NO_COLOR.

packaging/
  setup.py           py2app config. LSUIElement=True (no dock icon). Excludes mlx/scipy stubs.
  maramax_app.py     Bundle entry point. Adjusts sys.path for bundled vs dev mode.

assets/
  menu_icon.png      Menu bar icon (44x44 RGBA PNG).
```

### Threading Model

- **Main thread**: rumps event loop + AppKit UI. All NSView/NSPanel mutations must happen here.
- **Model loader thread**: `ParakeetTranscriber.__init__` spawns daemon thread to download/init model. Signals `ready_event` when done.
- **Recording thread**: `AudioRecorder._record_loop` monitors PyAudio callback stream in background.
- **Live preview worker**: `_live_preview_worker` feeds recorded PCM into `ParakeetTranscriber.stream_drafts` during recording, pushing draft text to the overlay (session-guarded).
- **Transcription workers**: `_transcribe_recording_worker` and `_process_queue_worker` run inference off main thread.

Thread coordination:
- `threading.Lock` protects mutable state (`AudioRecorder._state_lock`, `_stream_lock`; `HistoryStore._lock`; `DictationApp._state_lock`).
- `threading.Lock` also protects `TranscriptionQueue._lock` for queue item mutations.
- `threading.Event` for signaling (`ParakeetTranscriber.ready_event`, `DictationApp._cancel_event`, `DictationApp._queue_cancel_event`, `DictationApp._live_stop_event`).
- `AppHelper.callAfter()` marshals callbacks from worker threads to the main/AppKit thread.
- `threading.Timer` for delayed UI actions (status revert, copy feedback).

### Session Tracking

`DictationApp._overlay_session` is a monotonic counter incremented each time the overlay is shown. Workers receive the session value at spawn and check it before updating UI, preventing stale updates from cancelled/old operations.

### Cancellation

User clicks "Cancel" during transcription -> sets `_cancel_event` and `_queue_cancel_event` -> worker's progress callback checks the event and raises `TranscriptionError("Cancelled")` -> worker unwinds gracefully. Queue cancellation still exports any items that completed before the cancel.

**Invariant**: If `transcribe_pcm` returns text successfully, the result is always published — even if the cancel event was set while inference was running. Cancel only discards results when it actually interrupts inference (raises `TranscriptionError` via the chunk callback). A completed transcription is never thrown away.

### Deferred Overlay Actions

When transcription completes, two deferred flags may trigger post-completion actions:
- `_hide_after_transcription`: close overlay after transcription finishes.
- `_force_copy_after_transcription`: copy result to clipboard after completion.
Applied in `_finalize_deferred_overlay_actions()`.

### Model Strategy (Two Engines)

Two ASR models share the MLX/Metal runtime:

- **Parakeet TDT 0.6B v2** (always loaded, ~1.2 GB): live streaming drafts, transcription while the high-accuracy model loads, and fallback on any failure. Pinned to v2 — v3 regresses English WER.
- **Qwen3-ASR 1.7B** (`mlx-community/Qwen3-ASR-1.7B-bf16`, ~4 GB, loaded in background when the `high_accuracy` setting is on — **default off**: the ~5% relative WER gain reads as ~1 corrected word per 7-10 short messages, while costing ~1.5s per 10s of audio at stop vs Parakeet's ~0.3s; it shines on long files and formatting): final passes for mic dictation, single files, and queue items. Best English WER available on MLX (5.76 vs Parakeet's 6.05 on Open ASR). No streaming, no mid-inference cancellation (cancel is checked before inference; queue items remain cancellable between files; the completed-result-is-always-published invariant holds).

Routing lives in `DictationApp._final_transcribe_pcm/_final_transcribe_file`: Qwen when enabled+ready, otherwise Parakeet; any Qwen exception logs and falls back to Parakeet. Toggling the setting off calls `QwenTranscriber.unload()` to free RAM. First enable downloads ~3.4 GB from Hugging Face.

### Recording Start Latency

The first words of a dictation must not be lost; capture starts as early as possible:
- `AudioRecorder.warm_up()` runs at launch in a background thread (first CoreAudio open after process start is slow; `cleanup()` joins this thread — opening PortAudio during interpreter teardown segfaults).
- `start()` reuses the existing PyAudio session (rebuild only on open failure) — rebuilding each start cost ~100-200ms of speech.
- `_show_overlay_and_start_on_main` starts the mic **before** any overlay/window work.
- Status shows "Starting mic…" and flips to "Recording…" only when non-silent audio actually arrives (`signal_event`; Bluetooth mics deliver pure-zero frames for 1-2s while switching into headset mode — speech during that window is unrecoverable, so the indicator must not show early). Warm-up targets the built-in mic so launch never flips Bluetooth audio out of high-quality mode.
- `_audio_lock` serializes PyAudio session use (open/enumerate/reinit/terminate); device enumeration runs off the main thread to avoid beachballs during model load.

### Live Preview (Streaming Drafts)

While recording, `_live_preview_worker` feeds new PCM from `recorder.frames` into `ParakeetTranscriber.stream_drafts()` (parakeet-mlx `transcribe_stream`, ~1s batches), pushing growing draft text into the overlay. Drafts use local attention with limited context and are **less accurate** than the offline pass.

**Invariant**: the final transcription always comes from the offline `transcribe_pcm` pass over the full recording — drafts are display-only and are replaced on completion. The stream holds the shared encoder in streaming attention mode, so `_transcribe_recording_worker` joins the live thread (`_finish_live_preview`) before starting the offline pass. Option+Space acts as a toggle: pressing it during recording stops and finishes the dictation.

### Settings

`AppConfig` persists user-changeable fields (`auto_start_recording`, `auto_copy_to_clipboard`, `paste_to_active_app`, `live_preview`, `history_limit`) to `~/Library/Application Support/Maramax/settings.json` (atomic write). The menu bar Settings submenu toggles them; every toggle saves immediately. `AppConfig.load()` falls back to defaults for missing/corrupt/mistyped values.

### Auto-Paste

When `paste_to_active_app` is enabled, a successful mic transcription copy is followed by: hide overlay → re-activate the previously frontmost app (captured in `_capture_previous_app` before the overlay was shown) → post a synthetic Cmd+V via CGEvent (`autopaste.py`, ctypes CoreGraphics). Requires the Accessibility permission (`AXIsProcessTrusted`); enabling the setting without it opens System Settings and shows a status hint. Zero-length audio is guarded in `_transcribe_path` (it would crash the Metal encoder).

### Transcription Queue

File transcription uses a queue-based workflow. Dropping/picking files adds them to a `TranscriptionQueue` and switches the overlay to the Queue tab. Users can reorder (up/down buttons), remove, or clear items before starting. Clicking "Start" presents an `NSAlert` dialog asking for output mode:

- **Copy to Clipboard**: concatenates all results, copies once at the end.
- **Save as Individual Files (same directory)**: writes `filename.txt` next to each source file.
- **Save as Individual Files (choose directory)**: same naming, user picks target folder.
- **Save as Single File**: all transcripts concatenated into one file at a user-chosen path.

History entries are always created regardless of output mode. The queue worker (`_process_queue_worker`) processes items sequentially and uses `_queue_cancel_event` for cancellation. If cancelled mid-queue, any already-completed items are still exported.

The overlay's third segmented control tab ("Queue") shows a monospaced text list of items with status indicators and a count badge (e.g., "Queue (3)"). Selection for move/remove is cursor-position based in the text view. During processing, the queue list stays visible with live status updates per item, alongside a Cancel button — the overlay does not collapse to the minimal transcribing layout.

### MLX Memory Management

MLX uses a Metal buffer cache that holds GPU allocations between inference calls. Without cleanup, memory grows unboundedly across transcriptions. After each inference in `_transcribe_path`, the result is explicitly deleted, `gc.collect()` breaks cyclic references, and `mx.metal.clear_cache()` releases cached Metal buffers back to the OS. Model weights (referenced by `self.model`) survive the cache clear. Same cleanup runs after model warm-up.

### Thread Safety Rationale

Simple boolean/string flags (`recording_active`, `is_transcribing`, `overlay_visible`, `current_transcript`) are read/written without locks. This is safe because: (1) Python's GIL makes single-attribute reads/writes atomic, (2) all UI event handlers run on the main thread (AppKit serializes them), and (3) worker threads only write these flags at completion, then marshal UI updates via `AppHelper.callAfter`. The `_state_lock` protects compound state checks (e.g., `hide_overlay` reading multiple flags atomically) and flag groups that must change together (deferred overlay flags).

### Error Handling

Custom exceptions: `TranscriptionError`, `HotKeyError`, `ClipboardError`, `ExportError`. Pattern is graceful degradation — errors are logged, shown in status label, and the app continues. `AudioRecorder.last_error` and `ParakeetTranscriber.load_error` cache errors for deferred inspection.

### History Persistence

`HistoryStore` writes to `~/Library/Application Support/Maramax/history.json`. Uses atomic write (temp file + rename). Thread-locked. Auto-migrates from legacy `ParakeetDictation` directory. Limit configurable (default 100 entries).

## Key Constants

| Constant | Location | Value |
|---|---|---|
| Audio format | transcription.py | 16-bit PCM, mono, 16kHz, 512-frame chunks |
| Model ID (drafts/fallback) | transcription.py | `mlx-community/parakeet-tdt-0.6b-v2` (pinned: v3 regresses English WER) |
| Model ID (high accuracy) | transcription.py | `mlx-community/Qwen3-ASR-1.7B-bf16` |
| Chunk duration | transcription.py | 120s with 15s overlap |
| Live draft batch | transcription.py | ~1s of PCM per `stream_drafts` step, context (256, 256) |
| Settings file | config.py | `~/Library/Application Support/Maramax/settings.json` |
| FFmpeg timeout | transcription.py | 120s |
| Overlay width | overlay.py | 688px |
| Media extensions | overlay.py | aac, aiff, flac, m4a, mov, mp3, mp4, ogg, opus, wav, webm |
| Queue panel height | overlay.py | 310px |
| Bundle ID | packaging/setup.py | `com.maramax.dictation` |

## Environment Variables

| Variable | Purpose |
|---|---|
| `LOG_LEVEL` | Logging severity (default: INFO) |
| `NO_COLOR` | Disable colored log output |
| `TOKENIZERS_PARALLELISM` | Set to `false` in main.py to prevent numpy threading issues |
| `RESOURCEPATH` | Set by py2app at runtime for bundle resource resolution |

## Build & Deploy Workflow

Full build-deploy-push cycle:
```bash
uv sync --extra dev
bash build_app.sh
cp -R dist/Maramax.app /Applications/
rm -rf dist build
git add -A && git commit -m "message" && git push origin main
```

## Build Notes

The standalone .app is built with py2app. MLX is a namespace package with C extensions, which requires workarounds in `build_app.sh`:
1. Strip mlx/scipy/charset_normalizer bytecode stubs from py2app's zip (they shadow real packages).
2. Copy full mlx package to both `site-packages/` and `lib-dynload/` for C extension discovery.
3. Copy scipy and charset_normalizer as full packages.
4. Verify critical files exist in bundle before code signing.

## Dependencies

Runtime: `parakeet-mlx>=0.5.2,<0.6`, `qwen3-asr-mlx>=0.1.1,<0.2`, `numpy<2.3`, `pyaudio~=0.2.14`, `rumps~=0.4.0`, `pyperclip~=1.9.0`, `python-dotenv~=1.1.1`, `pyobjc-framework-cocoa~=11.1`.

Dev: `pytest`, `ruff`, `mypy`, `py2app`, `build`.

System: `portaudio`, `ffmpeg` (via Homebrew).

Requires Python 3.12 (pinned in pyproject.toml: `>=3.12,<3.13`). macOS 12+ on Apple Silicon recommended.
