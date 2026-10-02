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

## 0.5.0 reliability, cold start, and engine review — October 1, 2026

All 163 tests, Ruff, and mypy pass. `dist/Maramax.app` builds and passes the
packaged component check. The installed app was not replaced or launched, no
microphone was opened, and no sound was played. Everything below that concerns a
real microphone is therefore measured from existing recordings or exercised with
a simulated audio device, and is marked as such.

### What the existing recordings showed

Measured from the 20 archived dictations and the log of the installed 0.4.1:

| Input | Time to first audio buffer | Leading digital silence | Total before real audio |
| --- | ---: | ---: | ---: |
| MacBook Pro microphone (8 recordings) | 0.27–0.34 s | none | about 0.3 s |
| AirPods Pro 2 (12 recordings) | 0.41–0.47 s, five outliers of 0.8–3.1 s in the log | 1.44–2.55 s on every recording | about 1.9–3 s |

The silence is the headset changing into call mode; it arrives as exact zeros on
an already-open stream, so no change to how the stream is opened can remove it.
Two built-in-microphone recordings began mid-speech, and six of twenty ended
within 0.16 s of the last speech, close enough to clip a final syllable.

Start-up cost of the audio helper, measured without opening a device: launching
the packaged helper and getting its first answer takes 125 ms (median of six);
initializing PortAudio takes 37 ms cold.

### What changed and how it was checked

- **Standby helper.** The helper is launched ahead of time and reused, removing
  the 125 ms launch from every start. Checked with the real helper process and a
  simulated audio device, and with the packaged helper answering two requests
  on one process. Live effect on `first_frame_delay` not yet measured.
- **Capture starts before the bar is drawn.** Not separately measured.
- **0.2 s tail after stop.** Checked with the simulated device. Stop-to-result
  is unaffected because the helper now reports completion before it closes the
  device, where it previously made the app wait for the process to exit.
- **Mid-recording failover.** A simulated device that stops delivering is
  replaced by the new default input and the recording continues with audio from
  both; an explicitly selected microphone is not replaced. Not exercised with
  real AirPods.
- **Keep the microphone connected** (off by default). With the simulated device a
  second recording inside the window starts in under half a second on the open
  stream, audio heard while waiting is discarded, and the device is released when
  the window ends or the setting is turned off. Not exercised with real AirPods;
  this is the only change that can remove the 1.5–2.5 s of silence.
- **Closed lid.** Lid state read correctly on this Mac both open and closed
  (matching `ioreg`). That the built-in microphone is then skipped is covered by
  a unit test; it was not exercised with a live recording.

### Recognition

The standard engine now takes dictations of up to two minutes straight from
memory. Re-running all 20 archived dictations through the new path reproduced the
stored transcript exactly in every case:

| Dictation length | Recordings | Median before (as logged) | Median now |
| --- | ---: | ---: | ---: |
| under 30 s | 10 | 0.71 s | 0.29 s |
| 30 s to 2 min | 6 | 1.22 s | 0.87 s |
| over 2 min | 4 | 3.44 s | 3.09 s |

"Before" is what the installed app logged in normal use and includes whatever
else the Mac was doing; "now" was measured in one batch. The packaged engine
transcribed a public 10.4 s sample five times at 0.22–0.25 s
(`docs/validation/0.5.0-bundle.json`).

### Engine comparison on this Mac

Both engines transcribed the same 20 dictations (26 minutes of audio):

| Dictation length | Parakeet median | Qwen3-ASR 1.7B median | Qwen slowest |
| --- | ---: | ---: | ---: |
| under 30 s | 0.24 s | 2.1 s | 10.5 s |
| 30 s to 2 min | 0.85 s | 7.3 s | 9.5 s |
| over 2 min | 2.3 s | 19.5 s | 66.3 s |

The two disagree on 8.3 % of words. There is no reference transcript, so this is
not an error rate. Reading the differences in three dictations whose intended
wording is known: Qwen was right where Parakeet was wrong three times ("pop-up",
"cold start", "out for a year"), and Parakeet was right where Qwen was wrong at
least six times ("AirPods" heard as "EarPods", "address" as "dress", dropped
words). Given Qwen's `context` vocabulary, the name errors it had made
("Mara max", "EarPods") came out right. On the public leaderboard (version
25-09-2026) Qwen3-ASR 1.7B averages 4.31 % and Parakeet v2 4.70 %; no newer open
model with an MLX runtime ranks above Qwen.

Conclusion: no engine change is justified by this evidence. Parakeet stays the
default; the high-accuracy option now uses the user's word list as vocabulary.
Chunking stays at 120 s (processing a 5-minute dictation unchunked changed 19 of
1003 words with no sign of improvement and peaked at 6.9 GB). Beam search changed
0–4 words per dictation at three times the cost and is not used. Padding the end
with digital silence altered words mid-dictation, so the real captured tail is
used instead.

The high-accuracy model was run inside the packaged app with network access
disabled: it loaded from the cache in 1.8 s and transcribed the 10.4 s sample in
1.3–1.4 s, with and without a vocabulary hint.

### Next live checks

1. AirPods: compare `open_delay` and `first_frame_delay` in new recordings with
   the figures above.
2. Turn on **Keep the microphone connected for** and dictate twice within the
   window; the second should show `warm_start: true` and no leading silence.
3. In Automatic mode, take the AirPods out mid-dictation: the recording should
   continue on the Mac's microphone and the result should say it switched.
4. Dictate with the lid closed and the Mac-microphone preference on: the AirPods
   should be used.
