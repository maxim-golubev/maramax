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
  app.py             Core controller (DictationApp). Owns all state, coordinates components.
  overlay.py         Native NSPanel overlay: drop zone, device selector, text view, queue tab, controls.
  transcription.py   AudioRecorder (PyAudio callback streaming) + ParakeetTranscriber (model loading, chunked inference, FFmpeg normalization).
  queue.py           TranscriptionQueue (thread-safe item list), QueueItem dataclass, OutputMode/OutputConfig for save options.
  export.py          export_results() writes completed queue items to clipboard, individual files, or single file.
  hotkeys.py         GlobalHotKeyManager. Registers Option+Space via Carbon API ctypes bindings.
  history.py         HistoryStore. Thread-safe JSON persistence in ~/Library/Application Support/Maramax/. Auto-migrates legacy ParakeetDictation data.
  clipboard.py       copy_text() wrapper around pyperclip with ClipboardError.
  config.py          Frozen dataclasses: AppConfig (auto_start_recording, auto_copy, history_limit) and ShortcutConfig.
  paths.py           resource_path() resolves assets in dev vs bundle. ensure_runtime_path() prepends homebrew/bundle bins to PATH.
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
- **Transcription workers**: `_transcribe_recording_worker` and `_process_queue_worker` run inference off main thread.

Thread coordination:
- `threading.Lock` protects mutable state (`AudioRecorder._state_lock`, `_stream_lock`; `HistoryStore._lock`; `DictationApp._state_lock`).
- `threading.Lock` also protects `TranscriptionQueue._lock` for queue item mutations.
- `threading.Event` for signaling (`ParakeetTranscriber.ready_event`, `DictationApp._cancel_event`, `DictationApp._queue_cancel_event`).
- `AppHelper.callAfter()` marshals callbacks from worker threads to the main/AppKit thread.
- `threading.Timer` for delayed UI actions (status revert, copy feedback).

### Session Tracking

`DictationApp._overlay_session` is a monotonic counter incremented each time the overlay is shown. Workers receive the session value at spawn and check it before updating UI, preventing stale updates from cancelled/old operations.

### Cancellation

User clicks "Cancel" during transcription -> sets `_cancel_event` and `_queue_cancel_event` -> worker's progress callback checks the event and raises `TranscriptionError("Cancelled")` -> worker unwinds gracefully. Queue cancellation still exports any items that completed before the cancel.

### Deferred Overlay Actions

When transcription completes, two deferred flags may trigger post-completion actions:
- `_hide_after_transcription`: close overlay after transcription finishes.
- `_force_copy_after_transcription`: copy result to clipboard after completion.
Applied in `_finalize_deferred_overlay_actions()`.

### Transcription Queue

File transcription uses a queue-based workflow. Dropping/picking files adds them to a `TranscriptionQueue` and switches the overlay to the Queue tab. Users can reorder (up/down buttons), remove, or clear items before starting. Clicking "Start" presents an `NSAlert` dialog asking for output mode:

- **Copy to Clipboard**: concatenates all results, copies once at the end.
- **Save as Individual Files (same directory)**: writes `filename.txt` next to each source file.
- **Save as Individual Files (choose directory)**: same naming, user picks target folder.
- **Save as Single File**: all transcripts concatenated into one file at a user-chosen path.

History entries are always created regardless of output mode. The queue worker (`_process_queue_worker`) processes items sequentially and uses `_queue_cancel_event` for cancellation. If cancelled mid-queue, any already-completed items are still exported.

The overlay's third segmented control tab ("Queue") shows a monospaced text list of items with status indicators and a count badge (e.g., "Queue (3)"). Selection for move/remove is cursor-position based in the text view. During processing, the queue list stays visible with live status updates per item, alongside a Cancel button — the overlay does not collapse to the minimal transcribing layout.

### Error Handling

Custom exceptions: `TranscriptionError`, `HotKeyError`, `ClipboardError`, `ExportError`. Pattern is graceful degradation — errors are logged, shown in status label, and the app continues. `AudioRecorder.last_error` and `ParakeetTranscriber.load_error` cache errors for deferred inspection.

### History Persistence

`HistoryStore` writes to `~/Library/Application Support/Maramax/history.json`. Uses atomic write (temp file + rename). Thread-locked. Auto-migrates from legacy `ParakeetDictation` directory. Limit configurable (default 100 entries).

## Key Constants

| Constant | Location | Value |
|---|---|---|
| Audio format | transcription.py | 16-bit PCM, mono, 16kHz, 512-frame chunks |
| Model ID | transcription.py | `mlx-community/parakeet-tdt-0.6b-v2` |
| Chunk duration | transcription.py | 120s with 15s overlap |
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

Runtime: `parakeet-mlx`, `numpy<2.3`, `pyaudio~=0.2.14`, `rumps~=0.4.0`, `pyperclip~=1.9.0`, `python-dotenv~=1.1.1`, `pyobjc-framework-cocoa~=11.1`.

Dev: `pytest`, `ruff`, `mypy`, `py2app`, `build`.

System: `portaudio`, `ffmpeg` (via Homebrew).

Requires Python 3.12 (pinned in pyproject.toml: `>=3.12,<3.13`). macOS 12+ on Apple Silicon recommended.
