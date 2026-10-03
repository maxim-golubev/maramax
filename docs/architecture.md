# Maramax architecture

How the app is put together: what each module does, which thread does what, how audio, recognition, and updates work, and the numbers and file locations they rely on. What was measured is in [validation.md](validation.md).

Python menu-bar app (`rumps`) with native AppKit panels (`PyObjC`). Audio capture runs through PyAudio inside a helper process the app can replace. Global hotkeys use the Carbon API through ctypes. Recognition runs on MLX.

## Module Map

```
src/parakeet_dictation/
  main.py              Process entry point: starts the app or, with --audio-worker, the audio helper. Environment, logging,
                       instance guard, and signal handling are set up here and nowhere at import time.
  app.py               DictationApp (rumps.App): carries out each user action according to the current Phase. Owns the session
                       token, the worker threads, the status line, and the menu. Its rules are module-level pure functions
                       (empty_capture_outcome, queue_run_summary).

  isolated_recorder.py IsolatedAudioRecorder: the app's microphone. Keeps one audio helper on standby, bounds start and stop
                       with timeouts, replaces a helper that stops answering, spills PCM to the recovery file as it arrives.
  helper_protocol.py   The JSON-lines protocol between app and helper: Operation and Event enums, InputDevice.
  audio_worker.py      The helper process (`Maramax --audio-worker`): AudioHelper request loop — record, stop tail,
                       keep-warm, failover (route_failed is the pure decision), list, release, ping.
  recorder.py          AudioRecorder: PyAudio capture with the PortAudio wedge defenses (bounded locks, abandoned sessions,
                       zombie guards), reopen() (same recording, new device), rearm() (new recording, same stream), lid_closed().
                       Imported only by the helper: the app process never loads PortAudio.
  capture.py           CaptureMeter / CaptureSnapshot / CaptureHealth: what the stream is delivering and whether that is healthy.
  audio_format.py      The one PCM format (16 kHz mono 16-bit) and its arithmetic.

  transcription.py     ParakeetTranscriber (in-memory offline pass with chunking, and the draft stream it owns) and
                       QwenTranscriber (refcounted load/unload). normalize_media() converts imported media with FFmpeg.
  corrections.py       Word replacements (single pass, longest first) and vocabulary_hint() for the Qwen engine.

  recordings.py        RecordingStore: every capture archived as WAV + JSON metadata, pruned at 20 files / 512 MB.
                       RecordingStatus; recovery_candidate() picks what "Recover Last Recording" transcribes.
  recovery.py          Raw PCM spill files for crash recovery: recording-in-progress.pcm, and one unsaved-recording-<n>.pcm
                       per capture that did not reach the archive (0.6.x's last-recording.pcm is read as the oldest).
  history.py           HistoryStore: history.json (readable by 0.3.0) plus history-originals.json for pre-replacement text.
                       Source says where a transcript came from.
  config.py            AppConfig persisted to settings.json.
  atomic_file.py       write_text_atomically() (temp file, fsync, rename) and set_aside() for unreadable files.
  file_queue.py        TranscriptionQueue of media files, QueueStatus.
  export.py            Where a queue run's transcripts go (OutputMode / OutputConfig) and writing them there.

  indicator.py         DictationIndicator: the compact, non-activating bar. RoundIconButton draws its glyphs as geometry;
                       split_status() puts a long outcome on the bar's two lines.
  overlay.py           OverlayController: the full transcript window (Result / History / Queue). Frames are placed by
                       alignment rectangle (_place) from one set of constants.
  preferences.py       PreferencesController: the Settings window, laid out with NSStackView / NSGridView.
  recordings_window.py RecordingsController: playback, WAV export, Transcribe Again.
  main_thread.py       call_later(): delayed main-thread callbacks that also fire while a modal dialog is open.

  updater.py           Replacing the app with a newer GitHub release: latest_release() (asks GitHub) / newer_release() (pure),
                       download() (a delta when one is published for this version, else the whole app; SHA-256, bundle
                       identifier, version, and pinned signature checked), UpdatePaths, and swap_script() run by
                       install_after_exit() after the app quits. No AppKit.
  bundle_delta.py      The update delta between two bundles: make() (used by create_release.py) and apply(), which proves
                       the rebuilt tree equal to the delta's manifest. Directories, symlinks, and permissions included.
  update_offer.py      UpdateOffer: the "Check for Updates…" menu item, the daily check, the prompt (Install and Relaunch /
                       Later / Skip This Version), and quitting into the installer once the app is idle. Step says where it is.
  update_window.py     UpdateProgressWindow: download progress, "ready", "Restarting Maramax…", and Cancel.

  hotkeys.py           Shortcuts: shortcut_label() and shortcut_problem() (pure: names, and which combinations macOS
                       or apps already use), the presets (DEFAULT_DICTATE is Option+Space), STOP (Cmd+R), and the
                       GlobalHotKeyManager that registers them through Carbon (set_dictation_shortcut swaps safely).
  shortcut_picker.py   ShortcutPicker: the presets, or keys the user presses; used by Settings and the welcome.
  welcome.py           WelcomeController: the first launch's four steps (what it is, shortcut, copy or paste, try it).
  autopaste.py         PasteTarget (the last app used other than Maramax) and send_paste_keystroke().
  clipboard.py         copy_text() and contains_text() (fail-closed check before auto-paste).
  instance.py          InstanceLock: flock on app.lock; never unlinked.
  paths.py             resource_path(), app_bundle(), app_support_dir(), ensure_runtime_path(), ensure_ssl_certs().
  logger_config.py     `logger` (silent until main() calls setup_logging) and the rotating file log.

packaging/
  setup.py             py2app config. Version read from pyproject.toml. LSUIElement=True. Excludes mlx/scipy stubs.
  maramax_app.py       Bundle entry point; adjusts sys.path for bundle vs dev.
  check_bundle.py      Post-build check run inside the bundle's Python: isolated imports, TLS, the spectrogram front end,
                       hidden panels, archive round trip, optional recognition timing. Opens no audio device.
  create_release.py    Verifies the pinned signature, version, and source hashes, re-runs check_bundle, then copies the
                       app + LAUNCH.md into releases/ with build-info.json (commit, dirty flag), zips, writes SHA-256, and
                       writes a delta from the newest earlier certificate-signed release on this machine.
  publish_release.py   Uploads that ZIP, its deltas, and their SHA-256 files as GitHub release v<version> on HEAD, which
                       must be the clean, pushed commit build-info.json records; refuses a mismatched checksum or an
                       existing tag.
  create_signing_identity.sh  One-time: the release certificate, in its own keychain under ~/.maramax-signing.
  signing.sh           Where that keychain lives; sourced by build_app.sh and create_signing_identity.sh.
  Maramax.icon         The app icon as an Icon Composer document (the menu bar M, white on a graphite tile).
  app_icon.py          Renders it, with Xcode 26's actool and ictool, into Assets.car (macOS 26: Liquid Glass in every
                       appearance), Maramax.icns (earlier macOS, up to 1024 px), and the README's icon-light/dark.png.
                       The build only copies these committed outputs.

docs/
  LAUNCH.md            Copied into each release as START HERE.md.
  guide.md             Everything the app does and where it keeps things (the user guide).
  validation.md        What was verified for each version and which live hardware checks remain.
  images/              The README's animation and diagram, with the scripts that make them (dictation_bar.py renders
                       the bar off-screen with the app's own views; diagram.py writes the SVGs).
  validation/          Raw measurement JSON for the validated builds.
  clean-code.txt       The owner's code standard (kept locally, not published).
```

## Phase and Session

`DictationApp._phase` is one value: `IDLE`, `CONNECTING` (microphone requested, not yet open), `RECORDING`, or `TRANSCRIBING` (a dictation, a file, the queue, or a recovery). `recording_active`, `is_transcribing`, and `is_busy` are read-only views of it, so the combinations that used to be possible with separate flags cannot occur. It is read and written on the main thread only.

`_session` is bumped whenever a new operation takes ownership of the display (start of a recording, a file transcription, a recovery, or opening the window while idle). Every worker and delayed callback carries the value it started under and drops its result if the session moved on. `open_transcript_window()` deliberately does *not* bump it while an operation is in flight, so expanding the compact bar keeps the live drafts, the final result, and the original paste target.

## Threading Model

- **Main thread**: rumps event loop + AppKit. All view mutations and every change to `_phase`, `_session`, and `_hide_window_when_done` happen here. Workers hand over with `AppHelper.callAfter`; delayed work uses `main_thread.call_later` (never `AppHelper.callLater`, which stops firing while an alert or file panel is open, and never `threading.Timer`).
- **Model loader threads**: `ParakeetTranscriber.__init__` starts one immediately; `QwenTranscriber.start_loading()` starts one only after Parakeet is ready and only when `high_accuracy` is on.
- **Start worker** (`_start_recording_worker`): started before any window work in `start_recording()`. Calls `recorder.start(cancel)`, which sends a `record` request to the standby helper (spawning one if needed) and waits up to 8 s for `ready`. The `cancel` event is created per attempt, so a cancel can never leak into a later recording.
- **Helper reader thread** (`IsolatedAudioRecorder._read`): one per helper process, for that process's whole life. Parses helper stdout, appends PCM to `frames`, feeds the meter, writes the spill file. Output from a helper that has been replaced is ignored.
- **Capture monitor** (`_monitor_capture`): a 150 ms `call_later` loop on the main thread that updates the bar and status from `capture_snapshot().health`, notes a device change, and stops the recording on `MISSING`/`DISCONNECTED` or as soon as the recorder reports an error.
- **Draft stream**: owned by `ParakeetTranscriber.start_drafts()`. Runs only in the full window, never for the compact bar.
- **Transcription workers**: `_transcribe_recording_worker`, `_transcribe_file_worker`, `_transcribe_queue_worker`, `_recover_worker`. Each ends by scheduling `_complete_operation_on_main`, which is the only place the phase returns to `IDLE` after a transcription.
- **Update workers** (`UpdateOffer._check_worker`, `_download_worker`): network and disk only; they hand results back with `AppHelper.callAfter`. The daily check is a `call_later` loop on the main thread.

## The Main Thread Never Touches the Audio Driver

PortAudio calls can wedge indefinitely on Bluetooth route changes. Two layers defend against that:

1. **Process isolation.** The real PyAudio session lives in a child process launched from the app's own executable with `--audio-worker`. Start waits at most 8 s, stop at most 3 s; on timeout the helper is killed (`reset_count` increments), a fresh one is launched for the next recording, and the PCM already received stays in memory and in the spill file. If the parent dies, the helper sees EOF on stdin, gets 5 s to release the device, and `os._exit`s.
2. **In-process defenses** in `recorder.py` (inside the helper): bounded lock acquires, `_abandon_stream_session()` after a wedged close (fresh lock, zombie stream dropped, replacement PyAudio instance, old one never terminated because `Pa_Terminate` would free the wedged stream under the stuck call), and per-recording generation guards so a revived zombie callback cannot write into a newer recording.

A session that could not be shut down leaves PortAudio initialized in that process with its device list frozen. `AudioRecorder.cleanup()` reports this by returning False, and the helper then exits instead of taking another recording (`AudioHelper._release`); so does a helper whose kept-warm stream has gone dead, because closing a dead route is where PortAudio wedges.

Every stop path on the main thread only changes the phase; `recorder.stop()` runs inside `_transcribe_recording_worker`.

## Audio Helper Protocol

One helper process serves many recordings. `DictationApp._prepare_recorder()` launches it (no device is touched) once the model is ready and again after every recording, so the hotkey pays for the driver open only, not a ~125 ms process launch. Requests are JSON lines on stdin, events JSON lines on stdout; the names live in `helper_protocol.py`.

| Request | Events |
|---|---|
| `record` (`device`, `prefer_builtin`) | `ready` (`device`, `warm`) → `audio`… ; `error` then `idle` if the device cannot be opened |
| `stop` (`keep_warm` seconds) | 0.2 s more capture (`TAIL_SECONDS`), tail `audio`, `done`, then `warm` or `idle` |
| `list` (`prefer_builtin`) | `devices` (plus `automatic`: the input Automatic would use). Always a separate one-shot process, so a wedged enumeration cannot poison the standby helper. |
| `release` | `closing`, then `idle`: closes a stream that is being kept warm |
| `ping` | `pong` |

- `done` is sent before the device is closed: everything captured is already delivered, so `stop()` returns while the driver winds down. `IsolatedAudioRecorder._accepting` is set by `idle`/`warm` and cleared by `closing` and by sending a request; `start()` gives a helper that is not accepting 1 s, then replaces it. A helper that exits without answering a `record` is replaced once, transparently.
- **Failover** (`route_failed`): no buffer for 1.5 s, no buffer at all 4 s after open, or only zeros 6 s after open → `reconnecting`, `AudioRecorder.reopen()` (same frame list and meter, new device from a fresh PortAudio enumeration), `device` (`reopened`). A replacement stream is judged from counters taken when it opened; the app's meter likewise treats it as just opened, so it gets the app's no-buffer deadline, which outlasts the helper's. At most two successful reopens per recording; a reopen that fails ends the attempts, and the app finishes with what was captured. An explicitly selected microphone that is gone makes `reopen()` fail, so it is never swapped. These thresholds must stay below the app's own in `capture.py`; `tests/test_capture.py` asserts the order.
- **Keep warm** (`config.keep_mic_ready_seconds`, default 0, sent with `stop` so a change mid-recording applies): after `done` the stream stays open for that long with its audio discarded in the helper (`_tend_warm_stream`); a `record` with the same device, preference, and lid state (and, for Automatic, the same system default input, read live from CoreAudio by `default_input_device()` because PortAudio's copy is frozen) inside the window re-arms it (`rearm()`, which also restarts the meter) and reports `warm: true`. Changing a microphone setting sends `release`.

## Recording Pipeline (never lose audio)

Order inside `_transcribe_recording_worker`:

1. `recorder.stop()` returns the PCM. `_capture_at_stop` (taken on the main thread) supplies diagnostics.
2. **Archive first**: `RecordingStore.save()` writes the WAV + metadata before any inference, including silent and empty captures. A model returning no text never decides audio retention.
3. Digital silence (`not any(pcm)`) skips inference and reports a capture problem.
4. `_final_transcribe_pcm()` → `_final_pass()`: waits for the draft stream to release Parakeet's encoder, then routes to Qwen when enabled and ready (any Qwen failure or empty result falls back to Parakeet). If the draft stream never lets go, only Qwen can still produce a transcript; otherwise the worker fails and the audio is kept.
5. **Completed results are always published**, even if cancel was requested mid-inference. Cancel only discards work when it actually interrupted inference through the chunk callback; a late cancel still suppresses auto-paste.
6. `_settle_spill()` runs right after the archive attempt, before recognition: from then on the audio lives in exactly one place — the archive when it was written, otherwise an unsaved recording of its own (one file per dictation; none is ever replaced, and since keeping one is a rename there is no cap). Once the WAV is in place, `save()` returns the recording even if its metadata or pruning fails, so the spill is never kept as a second copy. (Keeping both produced a phantom "unsaved recording" at the next launch; a crash during recognition now leaves one recording with status `saved`.)
7. `finally`: the recording's metadata is updated with outcome (`RecordingStatus`), transcript, raw transcript, message, and timings (`stop_seconds`, `recognition_seconds`, `stop_to_result_seconds`, `audio_worker_resets`, `warm_start`, `active_threads`; the snapshot adds `open_delay` and `first_frame_delay`), the next standby helper is prepared, and completion is scheduled with how long the bar should stay up.

On launch a leftover spill is promoted, a status hint is shown once, and `_adopt_recovered_audio()` moves every unsaved recording into the archive, oldest first (status `saved`). **Recover Last Recording** uses `recovery_candidate()`: recordings still `saved` (never reached the recognizer), then the newest that is not `done`, then the newest unsaved recording that could not be adopted, then simply the newest. A recovery that returns no text marks the recording `failed`, so the next press moves on. **Transcribe Again** calls `transcribe_recording(id)` directly.

## Compact Bar vs Full Window

`config.compact_dictation` (default on) makes the hotkey show `DictationIndicator`, a `NSStatusWindowLevel` non-activating panel, so the target app keeps focus. In a compact session: no live preview, Cmd+R is registered as a global stop shortcut for the duration and released afterwards, auto-paste fires immediately without re-activating anything and is skipped if the frontmost app changed. The arrow button opens the full window; `_compact_session` is cleared but the session and paste target are preserved. With compact mode off, the full window opens, live preview runs, and auto-paste re-activates the previous app and waits 0.3 s before re-checking focus.

`dismiss_requested()` is what Escape, Close, Cmd+W, and the bar's button all call: it finishes a recording, cancels a transcription, or closes the window, by phase.

## Status Line

`_push_status(message, revert_after)` from any thread, `_show_status` on the main thread. A message of the form "Outcome — explanation" or "Outcome: explanation" fills the finished bar's two lines (`indicator.split_status`), so write problem statuses in that shape and keep each half short. A status with `revert_after` is temporary; when it expires the resting status returns (`_resting_status`, reset to "Ready" when an operation completes). The menu, the window, and the bar all mirror it. A bar that has already finished re-arms its hide timer when a later status arrives (for example "auto-paste skipped"), so that status is not cut short. Whether the window shows Record or Stop is derived from the phase, not passed in.

## Auto-Paste

Requires `paste_to_active_app`, a microphone transcript, and no cancel. Always copies first (even with the copy setting off), then on the main thread: Accessibility trust check (opens System Settings if missing), focus check, `contains_text()` clipboard check (fail closed), then `send_paste_keystroke()`. Both CGEvents are allocated before either is posted so a lone key-down can never be sent. The target is `PasteTarget.current()`, the last app activated other than Maramax, captured when a dictation starts. Known limit: the keystroke uses the physical V key, which is not Cmd+V on Dvorak-style layouts.

With paste off, "Copy the transcript to the clipboard" is honoured on every path; nothing forces a copy.

## Word Replacements

`config.replacements` is a list of `{heard, replacement}` rules (max 100, validated by `corrections.normalize_rules`). `apply_replacements` builds one alternation regex ordered longest-first, with a word boundary on each edge of a rule that is a word character, any run of whitespace between a rule's words, NFC normalization, and `re.IGNORECASE`; replacements never cascade. Applied to microphone and recovery transcripts only, never to imported media. The raw text is kept in `history-originals.json` and in the recording metadata.

## Model Strategy

- **Parakeet TDT 0.6B v2** (`mlx-community/parakeet-tdt-0.6b-v2`, pinned: v3 regresses English WER). Always loaded. Every capture goes from memory straight to `model.generate()` (`_transcribe_samples`): no temporary file and no FFmpeg. Above 120 s it is chunked (120 s, 15 s overlap) and merged with the library's own alignment functions, mirroring `parakeet_mlx`'s `transcribe()` except that it stops at the chunk that reaches the end (the library adds one more lying wholly inside that chunk's overlap, which can repeat words); before any repair below, outputs match the library path on all archived dictations. `_recognize` pads the input to where the library's last spectrogram frame reads, because the library sizes frames by FFT length but counts them by window length. Imported media is converted by `normalize_media()` first. `cached_model_source()` loads a complete Hugging Face cache snapshot as a local directory so startup makes zero HTTP requests.
- **Unformatted stretches are recognized again.** Parakeet sometimes stops formatting partway through a capture: lower-case "i", no capitals, no punctuation, until its window ends (8 of the last 100 transcripts, two of them imported files, mostly long). It is the encoder, not the decoder: resetting the decoder state reproduces it word for word, and whether a stretch collapses depends on exactly where its window starts. `collapsed_spans()` (pure) finds a run without punctuation of 12+ words that contains a lower-case "i", or of 40+ words without a single capital; a formatted run-on sentence keeps its capital I and is not touched. `_repair_collapses()` recognizes the stretch again in 20 s windows (4 s overlap, 5 s of context, a second try starting 15 s early), never the same window twice, and `repair_acceptable()` admits the result only if it is formatted and keeps at least 90 % of the words; `_splice()` merges it back with the library's own alignment. Spans are found again after each splice, at most 6 per capture. A cancel during the repair keeps the finished first pass. Imported media is repaired too. On the archive it fixed both affected captures (one changed word, "619" → "6:19", and a repeated "just" dropped) and left every other transcript byte-identical; beam search and shorter chunks everywhere were tried and were worse.
- **One encoder, two modes.** The draft stream switches the encoder to local attention until it ends, so `ParakeetTranscriber` owns the stream (`start_drafts` / `finish_drafts`) and refuses an offline pass while a stream still holds the encoder.
- **Qwen3-ASR 1.7B** (`mlx-community/Qwen3-ASR-1.7B-bf16`). Opt-in via Settings, loaded through `cached_model_source()` after Parakeet is ready; toggling off calls `unload()`, which defers `close()` while an inference is running. No progress callback, so cancel is checked before inference. Receives `vocabulary_hint(config.replacements)` as `context`. On audio with no speech it tends to answer with that context; `_without_echo` turns such an answer into "no text" so the Parakeet fallback decides, and `vocabulary_hint` admits only plain names and terms.
- Measured on this project's M3 Pro with real dictations (docs/validation.md): Parakeet runs at roughly 70× real time, Qwen at 5–20×, and neither was clearly more accurate on samples whose wording was known. Evaluated and not adopted: unchunked long audio, beam search, zero-padding the end of a capture beyond the last frame.

After every inference and warm-up: `del result`, `gc.collect()`, `mx.clear_cache()` to release Metal buffers.

## Microphone Selection

Automatic mode with `prefer_builtin_mic` (default on) picks the Mac's built-in microphone by name so AirPods stay an output device; otherwise the system default. The preference is skipped while `lid_closed()` (IOKit `AppleClamshellState`), because Apple silicon cuts the built-in microphone off in hardware then. An explicit `input_device` name is resolved strictly: if it is missing, start fails with "Selected microphone disconnected" rather than silently recording from another input. Microphone settings reach the recorder in one place, `DictationApp._configure_recorder()`. Device enumeration runs in a one-shot helper process and only when the Settings Microphone tab is on screen; the Automatic entry is labelled with the device it would open.

Bluetooth headsets deliver 1.5–2.5 s of exact zeros after the stream opens while they switch into call mode (measured on every archived AirPods recording). That is why the bar says "Waiting for microphone signal…" until non-zero samples arrive, and it is the delay the keep-warm setting exists to avoid. macOS 26/27 expose no API that shortens it for third-party macOS apps.

## Updates

`UpdateOffer` checks 60 s after launch and then every 24 h of the app running while `check_for_updates` is on (default), and whenever the menu item or Settings' **Check Now** calls `check_requested()`. It asks `https://api.github.com/repos/maxim-golubev/maramax/releases/latest`; the request carries the installed version in its User-Agent and nothing else. Settings → Updates shows `status_text()` and is told of every change through `on_change`. A newer tag retitles the menu item "Install Maramax <version>…" and, unless that version was skipped or the app is busy, opens the prompt (brought to the front; Return means **Later**, because it can appear while the user types elsewhere). A check the user asked for always answers (up to date, or the error). Downloading shows its percentage in the menu item and Settings only, never the status line, because that would land on the compact bar mid-dictation.

**Trust.** Releases are signed with a self-signed certificate made once by `packaging/create_signing_identity.sh` and kept in its own keychain under `~/.maramax-signing` (back it up: without it no installed copy accepts another update). `build_app.sh` signs with it when that keychain exists, else ad hoc with a warning. `codesign` finds an identity only on the user's keychain search list, which every app uses to find its passwords, so the signing step treats it with care: it refuses to start unless every entry is an existing keychain file, writes the original list to `~/.maramax-signing/search-list-before-signing` (a later build refuses to run while that file exists), appends the signing keychain, signs, restores the list on any exit, proves it identical, and locks the signing keychain. It also refuses a list that already holds the signing keychain. `create_signing_identity.sh` guards the list the same way around `security create-keychain`, which adds the new keychain to it, and deletes a half-made keychain when setup fails. `updater.SIGNER_CERTIFICATE_SHA1` pins it: `download()` refuses an app that does not satisfy `signer_requirement()` (`identifier … and certificate leaf = H"…"`), and `create_release.py` refuses to package one. The release's SHA-256 only catches a damaged download. A stable signing identity is also what lets macOS keep the microphone and Accessibility permissions across updates (0.6.0 was ad hoc, so moving to 0.6.1 resets them once).

**Deltas.** `create_release.py` also writes `Maramax-<new>-from-<old>.delta` against the newest earlier release on this machine signed with the release certificate: a ZIP of `files/` (what is new or changed) and `manifest.json` (what to delete, and the full tree of the new bundle: every directory, file, and symlink with its permissions), made by `bundle_delta.make()`. Before publishing it proves the delta by cloning the old app, applying it, and requiring the exact new tree and the pinned signature; the files to publish and their digests go into `releases/Maramax-<v>.assets.json`, and `publish_release.py` uploads exactly those. A copy at exactly `<old>` downloads the delta instead of the whole app: it clones the installed app (`cp -c`, instant on APFS) into `updates/download/assembled/`, clears its extended attributes (a browser-downloaded install carries quarantine), and `bundle_delta.apply()` deletes deepest first, places files and links (refusing any path outside the bundle or through a symlink), sets permissions, and compares the result with the manifest's tree; the pinned `codesign --verify --deep --strict` then checks the seal. Anything going wrong with a delta, including a broken delta asset, falls back to the whole app. The suffix is `.delta`, not `.zip`, because 0.6.1 accepts a release only with exactly one `.zip`. A delta also carries `deleted.txt`, the deletion list in the format 0.6.2's updater reads instead of the manifest (`create_release.py` writes it; remove it once no 0.6.2 copies remain). Deltas stay small because `build_app.sh` writes `python312.zip` reproducibly (it zeroes the build time py2app stamps into each `.pyc` header and fixes entry dates; the archive holds no `.py` for those timestamps to be checked against), does the same for `site.pyc`, and compiles the app's own bytecode afresh from the bundled sources as hash-checked `.pyc` instead of copying the checkout's `__pycache__` (whose files follow local file times and test runs); the bundled app never writes bytecode into itself (`sys.dont_write_bytecode` in `packaging/maramax_app.py`), which would break the seal. Two builds of one commit from the same virtual environment differ only in the signature (`MacOS/Maramax`, `CodeResources`); bytecode of the other packages is still copied from the environment.

**Progress.** **Install and Relaunch** opens `UpdateProgressWindow`: the bar follows `download()`'s `(received, expected)` reports (one per percent), then "Checking the download…", "Maramax <v> is ready" (with "restarts as soon as you finish dictating" while busy), and "Restarting Maramax…" for 0.8 s; then it looks once more (a dictation or dialog started during the notice sends it back to waiting), starts the swap, and quits. If the app is still running 15 s later, the quit did not happen: it says so and returns to idle. Cancel sets the download's own `threading.Event`: `download()` raises `UpdateCancelled` and deletes what it fetched, or a staged app waiting for idle is discarded. Choosing the menu item while it runs brings the window back.

**Install.** `ensure_installable()` refuses a read-only location or the kept rollback copy before anything is downloaded. `download()` fetches the `.zip.sha256` and the `.zip` (exactly one of each, both uploaded), compares the digest, unpacks with `ditto`, checks the bundle identifier, version, and pinned signature, and places the app beside the installed one as `.Maramax-update.app` (a rename on the same volume, else a copy); any failure deletes what it downloaded. Once the app has been idle on two looks 3 s apart with no dialog open, `install_after_exit()` starts `updates/download/install.sh` in its own session and the app quits. The script waits up to 60 s for the PID, then 1 s for LaunchServices; renames the installed app to `.Maramax-replaced.app` and the staged one into place (both in the same folder, every step checked); moves the old one to `updates/previous/Maramax.app` (or leaves it hidden beside the new one if that fails); writes an `InstallResult` to `updates/last-install`; and opens whichever app is installed, never one without an `Info.plist`. The next launch reads that result once: success shows in Settings, a failure as an alert. It logs to `logs/update.log`. Running from source (`app_bundle()` is None) the prompt points at the release page instead. The download carries no quarantine attribute, so Gatekeeper does not prompt.

## Settings

`AppConfig` persists `dictation_shortcut` ([key code, Carbon modifiers], refused back to Option+Space if a hand edit makes it unusable), `onboarded` (the welcome opens a second after launch until it has been seen once; closing it early counts), and: `auto_start_recording`, `auto_copy_to_clipboard`, `paste_to_active_app`, `live_preview`, `high_accuracy`, `history_limit`, `compact_dictation`, `prefer_builtin_mic`, `input_device`, `keep_mic_ready_seconds`, `use_corrections`, `replacements`, `check_for_updates`, `skipped_update_version`. Keys it does not recognize (written by a newer version) are carried through a save; so are unknown keys in recording metadata and history entries. A settings or history file that cannot be parsed is renamed to `*.corrupt` rather than overwritten.

`_SETTING_LABELS` in `app.py` names the checkboxes; the explanation under each is `_HELP` in `preferences.py`. The Settings window and the welcome talk to the controller through `current_shortcut`, `choose_shortcut`, `pause_shortcut`, `resume_shortcut` (the picker; the global shortcut is paused while new keys are recorded, and restored if the window closes), `set_paste_into_apps`, `paste_permitted`, `open_accessibility_settings`, `finish_welcome`, `toggle_setting`, `replace_word_rules`, `select_input_device`, `set_keep_microphone_ready`, `refresh_input_devices`, and `retry_speech_model`, and reads `config`, `is_busy`, `transcriber`, `qwen`, and `updates` (`status_text()`, `can_check()`, `check_requested()` for **Check Now**). The General tab opens with the app's name and version.

`DictationApp._install_edit_menu()` gives the (invisible) main menu an Edit submenu. Without it a menu-bar app's text fields ignore Cmd+V/C/X/A/Z.

## Error Handling

Custom exceptions: `TranscriptionError` (and `TranscriptionCancelled`, raised by progress callbacks), `HotKeyError`, `ClipboardError`, `ExportError`, `PasteError`, `UpdateError` (and `UpdateCancelled`), `ReleaseError`, `PublishError`. `InstallResult` is the swap script's outcome. Errors are logged, shown in the status line, and the app continues. Unexpected exceptions in workers are logged with their traceback (`logger.exception`). The broad `except` blocks that remain each guard "never lose audio" or "never hang the UI" and say so.

## Key Constants

| Constant | Location | Value |
|---|---|---|
| Audio format | audio_format.py | 16-bit PCM, mono, 16 kHz (512-frame buffers in recorder.py) |
| Helper start / stop timeout | isolated_recorder.py | 8 s / 3 s |
| Stop tail | audio_worker.py | 0.2 s |
| Failover triggers | audio_worker.py | 1.5 s without a buffer, 4 s with none at all, 6 s of only zeros; at most 2 reopens |
| Capture health thresholds | capture.py | counted from device open: no frame 5 s, stalled 3 s, quiet 10 s, reconnect limit 8 s; faint peak < 0.002 |
| Keep-warm choices | preferences.py / config.py | 0 (default), 30, 120, 300 s; settings accept 0–600 |
| Recording archive budget | recordings.py | 20 recordings / 512 MB, newest always kept |
| Min recoverable spill | recovery.py | 16000 bytes (~0.5 s) |
| Max replacement rules | corrections.py | 100 (heard ≤ 200 chars, replacement ≤ 2000; vocabulary terms ≤ 40) |
| Model IDs | transcription.py | `mlx-community/parakeet-tdt-0.6b-v2`, `mlx-community/Qwen3-ASR-1.7B-bf16` |
| Offline chunking | transcription.py | 120 s chunks, 15 s overlap, in memory |
| Unformatted-stretch repair | transcription.py | detect: 12 words with a lower-case "i", or 40 without a capital, and no punctuation; redo in 20 s windows, 4 s overlap, 5 s context (retry 15 s early); keep ≥ 90 % of words; at most 6 stretches per capture (`MAX_STRETCHES`) |
| Update checks | update_offer.py / updater.py | first 60 s after launch, then every 24 h; install after 2 idle looks 3 s apart; swap waits 60 s for the app to quit |
| Draft stream release | transcription.py | 30 s |
| FFmpeg timeout | transcription.py | 120 s |
| Shortcuts | hotkeys.py | dictation: `DEFAULT_DICTATE` Option+Space, chosen by the user (presets: Option+Space, Control+Shift+Space, Cmd+Shift+Space); `STOP` Cmd+R, only while recording |
| Log rotation | logger_config.py | 2 MB, 2 backups |
| Bundle ID | packaging/setup.py, main.py | `com.maramax.dictation` |

## Local Data

Everything lives under `~/Library/Application Support/Maramax/` (the directory is passed into `DictationApp`): `settings.json`, `history.json`, `history-originals.json`, `recordings/*.wav|json`, `recording-in-progress.pcm`, `unsaved-recording-<n>.pcm` (and `last-recording.pcm` left by 0.6.x), `app.lock`, `logs/maramax.log`, `logs/update.log`, `updates/download/` (an update being installed), `updates/last-install` (the swap's outcome, read once by the next launch), `updates/previous/Maramax.app` (the version replaced by the last update), and `*.corrupt` for a file that had to be set aside. Legacy `ParakeetDictation/history.json` is copied over on first run; Clear History deletes it, and the set-aside and interrupted-write copies of the history files, along with the history itself. Model weights stay in the Hugging Face cache.

## Environment Variables

| Variable | Purpose |
|---|---|
| `LOG_LEVEL` | Logging severity (default INFO) |
| `NO_COLOR` | Disable colored console output |
| `TOKENIZERS_PARALLELISM` | Forced to `false` in main() |
| `SSL_CERT_FILE` | Set by `ensure_ssl_certs()` to certifi's bundle when the default is unusable |
| `MARAMAX_SIGNING_DIR` | Where `build_app.sh` and `create_signing_identity.sh` keep the release signing keychain (default `~/.maramax-signing`; must be an absolute path) |
| `RESOURCEPATH` | Set by py2app; used for resource lookup, to find the helper executable, and to find the bundle an update replaces |

## Build Notes

MLX is a namespace package with C extensions, so `build_app.sh` strips the mlx/scipy/charset_normalizer stubs from py2app's zip, copies the full packages into `site-packages` (and mlx into `lib-dynload`), verifies critical files, rewrites `python312.zip` and `site.pyc` reproducibly, signs (see Updates), and runs `check_bundle.py`; a bundle that fails the check is moved to `dist/Maramax.app.failed-check`. The helper process is the same bundle executable, so a bundle must be able to start itself with `--audio-worker`; `check_bundle.py` sends it two pings on one process, which also proves it stays alive between requests. `create_release.py` refuses a stale bundle whose sources differ from the checkout and repeats the bundle check itself.

Release builds are signed with the self-signed release certificate (see Updates); without `~/.maramax-signing` the build is ad hoc and cannot be published. Neither is notarized: a copy downloaded with a browser has to be allowed once under System Settings → Privacy & Security. Public distribution without that step would need Developer ID signing and notarization.

## Dependencies

Runtime: `parakeet-mlx>=0.5.3,<0.6` (0.5.3 fixes out-of-order tokens after a chunk merge; `_recognize_in_chunks` mirrors its `transcribe()`), `qwen3-asr-mlx>=0.2.0,<0.3` (0.2.0 adds `context`), `numpy<2.3`, `pyaudio~=0.2.14`, `rumps~=0.4.0` (0.4.1 does not exist on PyPI), `pyperclip~=1.9.0`, `python-dotenv~=1.1.1`, `pyobjc-framework-cocoa~=11.1`.
Dev: `pytest`, `ruff`, `mypy`, `py2app`, `build`. System: `portaudio`, and `ffmpeg` via Homebrew for imported media only. Python pinned to 3.12.
