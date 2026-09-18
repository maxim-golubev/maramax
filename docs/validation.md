# Maramax 0.4.0 validation — September 4, 2026

The candidate at `dist/Maramax.app` builds and passes the checks below. The
installed app was not replaced, no microphone was opened, and no sound was
played. Live AirPods validation remains outstanding.

## Verified

- 127 automated tests pass, including 50 consecutive simulated recording sessions
  that release each worker, stream, and recovery file. Callbacks from previous
  sessions cannot contaminate the next recording.
- Insertion tests simulate changed focus, clipboard replacement, closed apps,
  newer sessions, shutdown, and unavailable permissions without posting actual
  keyboard events. Expanding an active operation preserves its original target.
  Keyboard-event allocation failures cannot post a lone key-down event.
- Lint and source type checks pass.
- Native startup is exercised in a separate process without model loads, hotkeys,
  windows being shown, or audio initialization. Settings edits survive reload.
- Model download failure/retry, duplicate instance locking, word replacements,
  original-transcript retention, and compatibility with older history readers pass.
- Cached standard-model startup succeeds with HTTP requests blocked, with zero
  requests attempted. Complete local weights are used directly without a network
  freshness check; incomplete or mismatched snapshots are not treated as ready.
- The bundle's signature verifies, and its executable reports version 0.4.0.
- All 23 bundled application source modules match the checkout.
- Required app, audio, UI, and recognition imports resolve inside the bundle,
  with development and system Python packages removed from the import path.
- Certificate loading, hidden settings/transcript/recovery/passive-panel creation, and temporary audio archive
  save/read/update/delete operations pass using the bundled runtime.
- The bundled Parakeet engine loads cached weights and transcribes files with
  network model access disabled. Qwen imports and simulated failure/cancellation
  paths pass; actual Qwen inference was not tested. The optional engine is off in
  the user's current settings and its weights are not cached.

## Recognition measurements

Machine: Apple M3 Pro, 36 GiB memory, macOS 15.7.9. Engine:
`mlx-community/parakeet-tdt-0.6b-v2`. Speech was synthesized directly to files by
macOS, then normalized to 16 kHz PCM. Nothing was played through an output device.

| Generated speech | Repetitions | Median recognition | Slowest recognition |
| --- | ---: | ---: | ---: |
| 6.69 seconds | 30 | 0.203 seconds | 0.212 seconds |
| 45.42 seconds | 3 | 0.679 seconds | 0.741 seconds |

Recognition timings include the application's temporary WAV handling, model
inference, and cache cleanup. They exclude model loading, microphone capture,
archive saving, main-thread scheduling, clipboard writes, and text insertion.
Model loading and warm-up took 2.04 and 1.69 seconds in these two recorded batches;
filesystem and operating-system caches were already warm.

Both clips produced consistent transcripts across repetitions. The short clip
rendered “three thirty” as “3:30”; the longer clip matched its supplied script.
Clean synthetic speech is a packaging and performance check, not an accuracy
benchmark for natural speech, accents, room noise, or AirPods.

After each of the 30 short recognitions, measured active MLX memory stayed at
1,277,343,940 bytes, the MLX cache was empty, and one Python thread remained.
Process peak resident memory stayed at 1,547,370,496 bytes in that batch. Peak
resident memory is a high-water mark, not a current allocation measurement;
Python's thread count does not include native library threads. These results
show no growth in these measurements over this small recognition-only run.
They do not rule out a driver leak or a problem that emerges over hours.

Raw results and source hashes (`source-sha256.json`) are under `docs/validation/`.
These measurements come from the final 0.4.0 build. The versioned release also
contains source hashes and a launch guide; its ZIP has a separate SHA-256 file.

## Next live checks

Run these when microphone use and potential Bluetooth playback changes are
convenient. Use the candidate with the prior app quit so their hotkeys do not
conflict; retain the prior app for rollback.

1. Built-in input: immediate speech, repeated dictations, and several minutes of
   continuous speech. Confirm captured duration and an audible saved recording.
2. Explicit AirPods input: repeat after reconnecting between recordings, then
   disconnect during a recording. Check the selected device and saved partial
   audio; distinguish missing audio from a recognizer returning no text.
3. Confirm the compact bar leaves the target app focused, Cmd+R becomes available
   to other apps after stopping, and optional insertion behaves correctly when
   focus changes during recognition.
4. Compare first-frame delay, stop time, recognition time, abandoned sessions,
   and thread counts across many live sessions. Separately time actual
   stop-to-visible-insertion latency; the current metadata does not measure it.
5. Check saved-recording playback, export, retry, and recovery after an interrupted
   capture. Confirm deletion removes the intended local recordings.

The existing engine is fast on these controlled clips. The next useful evidence
is from the capture and insertion path before considering a replacement engine
or adding another model for text cleanup.

## 0.4.1 recovery and interface update

All 132 tests, Ruff, mypy, source diff whitespace checks, and packaged native
component checks pass. The packaged launcher also successfully starts its audio
helper in a device-free ping mode, and resolves the helper command back to itself.

Real subprocess tests use synthetic PCM without opening a microphone. They verify
a bounded startup hang followed by eight successful fresh sessions, cancellation
before connection, an unexpected helper exit, and five minutes of PCM retained
byte-for-byte in memory and on disk after a frozen stop. These verify software
recovery behavior, not real AirPods interoperability.

Settings now has General, Microphone, and Words pages. Hidden native view checks
cover tab changes, microphone selection, unavailable selections, and saved word
replacement editing. Offscreen renders were visually checked for all three pages.

The packaged standard model transcribed the existing 45.42-second synthetic clip
three times with a median of 0.750 seconds and maximum of 0.760 seconds. This
excludes capture, UI, clipboard, and insertion. Downloads were disabled. Raw
results are in `docs/validation/0.4.1-bundle.json`. No microphone was opened and
no audio was played. Live AirPods reconnect and real multi-minute dictation
remain the next user checks.
