# Maramax

On-device dictation for macOS on Apple Silicon. Option+Space records the microphone, NVIDIA Parakeet (via MLX) transcribes locally, and the result lands on the clipboard with optional auto-paste. Audio never leaves the machine. The Python package is still named `parakeet_dictation`; the product, bundle, and support directory are `Maramax`.

User-facing behaviour is documented in `README.md` and `docs/LAUNCH.md`; this file covers the code.

## Quick Start

```bash
brew install portaudio ffmpeg
uv sync --extra dev
./run.sh
```

Build the standalone app, then install and release:

```bash
bash build_app.sh                               # dist/Maramax.app + bundle check (no mic, no UI)
cp -R dist/Maramax.app /Applications/
.venv/bin/python packaging/create_release.py    # releases/Maramax-<version>/ + ZIP + SHA-256
```

First launch downloads the Parakeet weights (~1.2 GB in memory once loaded). The optional Qwen3-ASR engine (~4.1 GB download) is off by default and loads only when enabled in Settings.

## Checks

```bash
.venv/bin/python -m pytest -q          # 132 tests, ~4s; synthetic PCM, fake audio backend, no model weights
.venv/bin/python -m ruff check src/ tests/
.venv/bin/python -m mypy src/
.venv/bin/python packaging/check_bundle.py [--audio speech.wav --repeats 10 --output report.json]
```

Tests never open a microphone, play sound, show a window, or download weights. `tests/test_isolated_recorder.py` spawns the real audio-helper subprocess with a fake PyAudio. `tests/test_startup.py` constructs the native app in a separate process. Keep new tests in that style: `tmp_path`, monkeypatching, no heavy mocking frameworks.

## Architecture

Python menu-bar app (`rumps`) with native AppKit panels (`PyObjC`). Audio capture runs through PyAudio inside a disposable helper process. Global hotkeys use the Carbon API through ctypes. Recognition runs on MLX.

### Module Map

```
src/parakeet_dictation/
  main.py              Entry point. --version, --audio-worker dispatch, running-app + flock instance guard, signal handlers.
  app.py               DictationApp (rumps.App). Owns all state, session counters, engine routing, every worker thread, menu callbacks.

  isolated_recorder.py IsolatedAudioRecorder: the recorder the app uses. Spawns the audio helper, streams PCM over stdout JSON,
                       bounds start/stop with timeouts, spills PCM to the recovery file as it arrives.
  audio_worker.py      The helper process (`Maramax --audio-worker`). No GUI, no model. Wraps recorder.AudioRecorder.
  recorder.py          In-process PyAudio recorder with all PortAudio wedge defenses (bounded locks, abandoned sessions, zombie guards).
  capture.py           CaptureMeter / CaptureSnapshot: per-recording level, peak, first-frame delay, and a health state.

  transcription.py     ParakeetTranscriber (background load, offline chunked pass, streaming drafts) and QwenTranscriber
                       (refcounted load/unload, no cancellation). cached_model_source() avoids network freshness checks.
  corrections.py       Explicit word replacements: single pass, longest phrase first, word boundaries, case-insensitive.

  recordings.py        RecordingStore: every capture archived as WAV + JSON metadata, pruned at 20 files / 512 MB, newest always kept.
  recovery.py          Raw PCM spill files (recording-in-progress.pcm / last-recording.pcm) for crash recovery.
  history.py           HistoryStore: history.json (readable by 0.3.0) plus history-originals.json for pre-replacement text.
  config.py            AppConfig persisted to settings.json (atomic write, type-validated load). Frozen ShortcutConfig.
  queue.py / export.py TranscriptionQueue + export_results() for batch media-file transcription.

  indicator.py         DictationIndicator: the compact, non-activating dictation bar (status, mic name, timer, level meter).
  overlay.py           OverlayController: full window with Result / History / Queue tabs, drag-and-drop, output-mode dialog.
  preferences.py       PreferencesController: Settings panel with General / Microphone / Words tabs.
  recordings_window.py RecordingsController: playback, WAV export, Transcribe Again.

  hotkeys.py           GlobalHotKeyManager. Option+Space always; Cmd+R registered only while a compact recording is active.
  autopaste.py         accessibility_trusted() + send_paste_keystroke() (CGEvent Cmd+V, both events allocated before posting).
  clipboard.py         copy_text() and contains_text() (fail-closed check before auto-paste).
  instance.py          InstanceLock: flock on app.lock; never unlinked.
  paths.py             resource_path(), app_support_dir(), ensure_runtime_path(), ensure_ssl_certs().
  logger_config.py     Colored console logging + 2 MB rotating file log (2 backups). LOG_LEVEL / NO_COLOR.

packaging/
  setup.py             py2app config. Version read from pyproject.toml. LSUIElement=True. Excludes mlx/scipy stubs.
  maramax_app.py       Bundle entry point; adjusts sys.path for bundle vs dev.
  check_bundle.py      Post-build check run inside the bundle's Python: isolated imports, TLS, hidden panels, archive round trip,
                       optional recognition timing against a cached model. Opens no audio device.
  create_release.py    Verifies signature/version/source hashes, copies the app + LAUNCH.md into releases/, zips, writes SHA-256.

docs/
  LAUNCH.md            Copied into each release as START HERE.md.
  validation.md        What was verified for 0.4.x and which live hardware checks remain.
  validation/          Raw measurement JSON and source hashes for the validated builds.
```

### Threading Model

- **Main thread**: rumps event loop + AppKit. All NSView/NSPanel mutations happen here. Workers marshal UI updates with `AppHelper.callAfter`; delayed UI actions use `AppHelper.callLater`, never `threading.Timer`.
- **Model loader threads**: `ParakeetTranscriber.__init__` starts one immediately; `QwenTranscriber.start_loading()` starts one only after Parakeet is ready and only when `high_accuracy` is on.
- **Start worker** (`_start_recording_worker`): calls `recorder.start()`, which spawns the audio helper and waits up to 8 s for its `ready` event. The main thread shows "Connecting microphone…" meanwhile and can cancel via `cancel_start()`.
- **Helper reader thread** (`IsolatedAudioRecorder._read`): parses helper stdout, appends PCM to `frames`, feeds the meter, writes the spill file.
- **Capture monitor** (`_monitor_capture`): a 150 ms `callLater` loop on the main thread that updates the bar and status from `capture_snapshot().health`, and stops the recording on `missing`/`disconnected`.
- **Live preview worker**: feeds new PCM into `ParakeetTranscriber.stream_drafts`. Runs only in the full window, never for the compact bar.
- **Transcription workers**: `_transcribe_recording_worker`, `_transcribe_file_worker`, `_process_queue_worker`, `_recover_worker`.

`_state_lock` guards compound state checks and the deferred overlay flags. Simple flags (`recording_active`, `is_transcribing`, `overlay_visible`, `current_transcript`) are written on the main thread or at worker completion and rely on the GIL for atomic reads.

### Session Tracking

`_overlay_session` is bumped whenever a new operation takes ownership of the display (start of a recording, file transcription, recovery, or showing the overlay while idle). Every worker and every delayed callback captures the session at spawn and drops its result if the session moved on. `_show_overlay_on_main` deliberately does *not* bump the session while an operation is in flight, so expanding the compact bar keeps the live drafts, the final result, and the original auto-paste target.

### The Main Thread Never Touches the Audio Driver

PortAudio calls can wedge indefinitely on Bluetooth route changes. Two layers defend against that:

1. **Process isolation.** The app's recorder is `IsolatedAudioRecorder`; the real PyAudio session lives in a child process launched from the app's own executable with `--audio-worker`. Start waits at most 8 s, stop at most 3 s; on timeout the helper is killed (`reset_count` increments) and the PCM already received stays in memory and in the spill file. If the parent dies, the helper sees EOF on stdin and `os._exit`s.
2. **In-process defenses** in `recorder.py` (still used inside the helper): bounded lock acquires, `_abandon_stream_session()` after a wedged close (fresh lock, zombie stream dropped, replacement PyAudio instance, old one never terminated because `Pa_Terminate` would free the wedged stream under the stuck call), per-recording identity guards so a revived zombie callback cannot write into a newer recording, and a hard stop after two abandoned sessions.

Every stop path on the main thread only flips flags; `recorder.stop()` runs inside `_transcribe_recording_worker`.

### Recording Pipeline (never lose audio)

Order inside `_transcribe_recording_worker`:

1. `recorder.stop()` returns the PCM. `_capture_at_stop` (taken on the main thread) supplies diagnostics.
2. **Archive first**: `RecordingStore.save()` writes the WAV + metadata before any inference, including silent and empty captures. A model returning no text never decides audio retention.
3. Digital silence (`not any(pcm)`) skips inference and reports a capture problem.
4. `_finish_live_preview()` joins the preview thread (30 s). If it wedged, the shared Parakeet encoder is stuck in streaming attention mode, so the offline pass must not run; Qwen rescues the dictation if loaded, otherwise the worker fails and the audio is kept.
5. `_final_transcribe_pcm()` routes to Qwen when enabled and ready, else Parakeet; any Qwen failure falls back to Parakeet.
6. **Completed results are always published**, even if cancel was requested mid-inference. Cancel only discards work when it actually interrupted inference through the chunk callback.
7. `finally`: the recording's metadata is updated with outcome, transcript, raw transcript, message, and timings (`stop_seconds`, `recognition_seconds`, `stop_to_result_seconds`, `audio_worker_resets`, `active_threads`).

Recovery spill: the in-progress PCM file is discarded once the archive exists, otherwise promoted to `last-recording.pcm`. On launch a leftover in-progress file is promoted and a status hint shown. **Recover Last Recording** prefers the newest archived recording that is not `done`, then the legacy spill file. **Transcribe Again** in the Recordings window calls the same path with an explicit id.

### Compact Bar vs Full Window

`config.compact_dictation` (default on) makes Option+Space show `DictationIndicator`, a `NSStatusWindowLevel` non-activating panel, so the target app keeps focus. In a compact session: no live preview, Cmd+R is registered as a global stop shortcut for the duration and released afterwards, auto-paste fires immediately without re-activating anything and is skipped if the frontmost app changed. The arrow button expands to the full overlay; `_compact_session` is cleared but the session and paste target are preserved. With compact mode off, the full overlay opens, live preview runs, and auto-paste re-activates the previous app and waits 0.3 s before re-checking focus.

### Auto-Paste

Requires `paste_to_active_app` and a microphone transcript. Always copies first (even with auto-copy off), then on the main thread: Accessibility trust check (opens System Settings if missing), focus check, `contains_text()` clipboard check (fail closed), then `send_paste_keystroke()`. Both CGEvents are allocated before either is posted so a lone key-down can never be sent.

### Word Replacements

`config.replacements` is a list of `{heard, replacement}` rules (max 100, validated by `corrections.normalize_rules`). `apply_replacements` builds one alternation regex ordered longest-first with `(?<!\w)…(?!\w)` boundaries and `re.IGNORECASE`, so replacements never cascade. Applied to microphone and recovery transcripts only, never to imported media. The raw text is kept in `history-originals.json` and in the recording metadata.

### Model Strategy

- **Parakeet TDT 0.6B v2** (`mlx-community/parakeet-tdt-0.6b-v2`, pinned: v3 regresses English WER). Always loaded. Offline pass uses 120 s chunks with 15 s overlap; streaming drafts use `transcribe_stream(context_size=(256, 256))` on ~1 s batches. `cached_model_source()` loads a complete Hugging Face cache snapshot as a local directory so startup makes zero HTTP requests.
- **Qwen3-ASR 1.7B** (`mlx-community/Qwen3-ASR-1.7B-bf16`). Opt-in via Settings. Background load after Parakeet is ready; toggling off calls `unload()`, which defers `close()` while an inference is running (`_active_inferences`). No progress callback, so cancel is checked before inference.

After every inference and warm-up: `del result`, `gc.collect()`, `mx.clear_cache()` to release Metal buffers.

### Microphone Selection

Automatic mode with `prefer_builtin_mic` (default on) picks the Mac's built-in microphone by name so AirPods stay an output device; otherwise the system default. An explicit `input_device` name is resolved strictly: if it is missing, start fails with "Selected microphone disconnected" rather than silently recording from another input. Device enumeration runs in the helper process and only when the Settings Microphone tab is visible.

### Settings

`AppConfig` persists: `auto_start_recording`, `auto_copy_to_clipboard`, `paste_to_active_app`, `live_preview`, `high_accuracy`, `history_limit`, `compact_dictation`, `prefer_builtin_mic`, `input_device`, `use_corrections`, `replacements`. The `_SETTING_LABELS` dict in `app.py` drives both the hidden rumps menu items (kept as the single toggle path) and the Settings panel checkboxes; `PreferencesController.toggleSetting_` forwards to `DictationApp._on_setting_toggled`.

### Error Handling

Custom exceptions: `TranscriptionError`, `HotKeyError`, `ClipboardError`, `ExportError`, `PasteError`. Errors are logged, shown in the status line (menu, overlay, and compact bar all mirror `_apply_status_on_main`), and the app continues. `revert_after` statuses use a token so a stale revert cannot overwrite a newer message.

## Key Constants

| Constant | Location | Value |
|---|---|---|
| Audio format | recorder.py / audio_worker.py | 16-bit PCM, mono, 16 kHz, 512-frame chunks |
| Helper start / stop timeout | isolated_recorder.py | 8 s / 3 s |
| Capture health thresholds | capture.py | waiting < 5 s, disconnected > 3 s without frames, quiet > 10 s without signal |
| Recording archive budget | recordings.py | 20 recordings / 512 MB, newest always kept |
| Min recoverable spill | recovery.py | 16000 bytes (~0.5 s) |
| Max replacement rules | corrections.py | 100 (heard ≤ 200 chars, replacement ≤ 2000) |
| Model IDs | transcription.py | `mlx-community/parakeet-tdt-0.6b-v2`, `mlx-community/Qwen3-ASR-1.7B-bf16` |
| Offline chunking | transcription.py | 120 s chunks, 15 s overlap |
| FFmpeg timeout | transcription.py | 120 s |
| Live preview join | app.py | 30 s |
| Log rotation | logger_config.py | 2 MB, 2 backups |
| Bundle ID | packaging/setup.py | `com.maramax.dictation` |

## Local Data

Everything lives under `~/Library/Application Support/Maramax/`: `settings.json`, `history.json`, `history-originals.json`, `recordings/*.wav|json`, `recording-in-progress.pcm`, `last-recording.pcm`, `app.lock`, `logs/maramax.log`. Legacy `ParakeetDictation/history.json` is copied over on first run. Model weights stay in the Hugging Face cache.

## Environment Variables

| Variable | Purpose |
|---|---|
| `LOG_LEVEL` | Logging severity (default INFO) |
| `NO_COLOR` | Disable colored console output |
| `TOKENIZERS_PARALLELISM` | Forced to `false` in main.py |
| `SSL_CERT_FILE` | Set by `ensure_ssl_certs()` to certifi's bundle when the default is unusable |
| `RESOURCEPATH` | Set by py2app; used for resource lookup and to find the helper executable |

## Build Notes

MLX is a namespace package with C extensions, so `build_app.sh` strips the mlx/scipy/charset_normalizer stubs from py2app's zip, copies the full packages into `site-packages` (and mlx into `lib-dynload`), verifies critical files, ad-hoc signs, and runs `check_bundle.py`. The helper process is the same bundle executable, so a bundle must be able to start itself with `--audio-worker`; `check_bundle.py` pings it. `create_release.py` refuses a stale bundle whose sources differ from the checkout.

The app is ad-hoc signed for local use. Public distribution would need Developer ID signing and notarization.

## Dependencies

Runtime: `parakeet-mlx>=0.5.2,<0.6`, `qwen3-asr-mlx>=0.1.1,<0.2`, `numpy<2.3`, `pyaudio~=0.2.14`, `rumps~=0.4.1`, `pyperclip~=1.9.0`, `python-dotenv~=1.1.1`, `pyobjc-framework-cocoa~=11.1`.
Dev: `pytest`, `ruff`, `mypy`, `py2app`, `build`. System: `portaudio`, `ffmpeg` via Homebrew. Python pinned to 3.12.
