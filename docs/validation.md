# Maramax validation log

What was verified for each version, newest last.

## 0.4.0 — September 4, 2026

The candidate at `dist/Maramax.app` builds and passes the checks below. The
installed app was not replaced, no microphone was opened, and no sound was
played. Live AirPods validation remains outstanding.

### Verified

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

### Recognition measurements

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

### Next live checks

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

## 0.5.1 independent audit and restructuring — October 1, 2026

0.5.0 shipped after a single-author review. For 0.5.1 the code was audited by
fresh code-review agents that had not seen the author's reasoning: four correctness
audits (audio layer, controller, native UI, recognition/storage/packaging) and
one review against the owner's Clean Code standard, then a second round by two
fresh reviewers over the fixes. All 226 tests, Ruff, and mypy pass; the bundle
builds and passes its check. As before, no microphone was opened and nothing was
played: hardware behaviour is still verified only with a simulated audio device.

### What the first round found in 0.5.0, and what was done

High severity:

- **A stuck Bluetooth close left the reused audio helper permanently broken.**
  PortAudio keeps its device list until every session is shut down; a wedged
  session can never be, so every later recording in that process looked for the
  device that had vanished. The helper now exits whenever its audio session
  could not be released cleanly, and the app starts a fresh one. Covered by a
  test with a helper whose session leaks.
- **The high-accuracy model could output its own vocabulary list.** Reproduced
  with the real model on four noise-only clips (room noise, hum, key clicks, a
  short tap): each returned the "Vocabulary: …" context verbatim, which would
  have been copied and pasted. The app now treats an answer that repeats the
  context as "no text", so the standard engine decides; the same four clips then
  give no text and real speech is unaffected. Only plain names and terms are
  admitted to the context (no snippets, addresses, lists, or numbers).

  *Note, October 2, 2026 (after 0.6.3):* an answer made only of vocabulary
  terms is no longer always dropped, since a user can say exactly those words.
  It is kept when the standard engine also hears speech in the capture; an
  answer that repeats the context with its "Vocabulary:" label is still no text.

Audio loss or wrong destination:

- Confirming "Clear History & Recordings" after a dictation had started behind
  the dialog deleted that dictation's audio. The busy check is now repeated
  after the dialog.
- Cancelling during transcription still pasted the result into the other app
  when the transcript completed anyway. It is no longer pasted.
- With a Maramax window in front, auto-paste could target an app the user had
  left earlier. The target is now the last app activated other than Maramax.
- A disk-full error while spilling audio ended the recording early. Spilling
  now stops and the recording continues in memory.

Reliability:

- A stream that opened but never delivered was reopened at 4 s and then
  declared missing at 5 s anyway; a failed reopen was shown as "Recording";
  a cancel could abort the following dictation in two more ways than the one
  fixed in 0.5.0; a helper that was slow to die could strand the recorder.
- Every failed, empty, or cancelled dictation was kept twice and came back as
  "Unsaved recording found" at the next launch. The audio now lives in one
  place, settled before recognition starts.
- Dictations over two minutes needed FFmpeg. All dictation is now recognized
  from memory; outputs matched the stored transcripts on all 20 archived
  dictations (5–588 s, six of them chunked).
- Meters, the disconnect watchdog, and the bar's auto-hide stopped while any
  dialog was open (confirmed in isolation: `AppHelper.callLater` does not fire
  in the modal run-loop mode; a common-modes timer does).
- A file written by a newer version lost its transcript, history entry, or
  settings when an older version saved it; an unreadable settings or history
  file was overwritten. Unknown fields are now carried through, and unreadable
  files are renamed aside.
- `kill` was ignored while the app sat idle.

Interface (measured off-screen):

- The bar's status text was cut off in most finished states. The bar is wider
  and a finished bar puts the outcome and its explanation on separate lines.
- Queue Remove and the arrows acted on an invisible cursor (Remove with nothing
  clicked removed the last file). The chosen file is now highlighted, and the
  buttons are disabled until one is chosen; a filename containing an emoji no
  longer shifts the choice by a line.
- Settings and Recordings disappeared when another app was clicked; windows
  jumped back to the centre whenever they were shown; the reopened transcript
  window had a blank status.
- Visible edges of controls sat 2–6 pt off the margins because frames, not
  alignment rectangles, were lined up. Measured after the change: left and
  right edges at the margins in every state of the transcript window and in
  Recordings, one centre line per row, and a 24 pt bottom margin on all three
  Settings tabs.

Structure (Clean Code review): one `Phase` value replaced four boolean flags;
the audio format, the helper protocol's names, shortcut names, recording and
queue statuses, and capture health each have one definition; the recorder's
unused spill code and other dead code were deleted; import-time work was
removed (importing the package no longer reads the environment or configures
logging, and the app process no longer loads PortAudio); decisions that were
buried in the controller are pure functions with table tests. `app.py` went
from 1,547 lines to 1,387.

### Second round

Two fresh review agents re-audited the result. They confirmed the fixes above, with
these exceptions, all since addressed: two controller calls no longer matched
the bar's signature after a late change (would have raised on a failed
microphone start); the helper-retry had a race the tests hit about once in 14
runs (now 0 in 20); a draft stream was left running after a silent capture;
dying during recognition could still duplicate a recording; emoji filenames
shifted the queue selection.

### Known and left as is

- Auto-paste sends the physical V key, which is not Cmd+V on Dvorak-style
  layouts. *(Note, October 2, 2026, after 0.6.3: fixed. The paste keystroke
  and the Cmd+R stop shortcut now use the key that gives V or R with Command
  on the current layout.)*
- The reader side of the helper protocol still uses field names as plain
  strings.
- `app.py` remains the largest module. The reviewers' advice, which matches the
  standard, was to stop at the phase model and the extracted decisions rather
  than split it into files that hide nothing.
- With both "Copy the transcript to the clipboard" and "Paste into the active
  app" off, a dictation finished with the hotkey now leaves the text only in
  History and the transcript window. 0.5.0 copied it regardless of the setting.

### Still to verify on real hardware

The four checks listed for 0.5.0 stand. Add: with AirPods, let a dictation run
while they disconnect (the result should say the microphone changed), and
confirm that the next dictation after any microphone failure starts normally.

## 0.6.0 self-update and unformatted transcripts — October 2, 2026

All 269 tests, Ruff, and mypy pass. No microphone was opened and nothing was
played; recognition was measured on the 20 dictations already in the archive.

### Transcripts that lose their capitals and punctuation

8 of the last 100 transcripts in history contain a stretch written as
"so i would like you to make it sure that it actually goes through bold if…":
lower-case "i", no capitals, no punctuation, for 40 to 324 words, mostly in long
dictations. Two of those are still in the archive (75 s and 588 s, both AirPods).

What it is: the encoder, not the decoder and not chunking. The 75 s capture is a
single window; resetting the decoder's state where the stretch begins
reproduced the same words exactly. Recognizing the same audio in a slightly
different window usually comes out formatted, sometimes not: of six 40 s windows
over the two stretches three collapsed, of six 20 s windows none did.

What was tried, on all 20 archived dictations:

| Approach | Fixed both | Side effects |
| --- | --- | --- |
| Beam search (2, 3, 5) | No: fixed the 75 s one; beam 5 made the 588 s one worse (4 → 12 lower-case "i") | 1.4–3× slower everywhere |
| 20 s chunks for everything | No | New collapses in two dictations that had none |
| Local attention | No | — |
| Detect the stretch, recognize it again in 20 s windows, splice it in | Yes | Nothing else changed |

The last is what 0.6.0 does. A stretch is a run of at least 12 words without
punctuation that contains a lower-case "i", or of at least 40. It is recognized
again with 5 s of context in 20 s windows overlapping by 4 s (and once more
starting 10 s earlier if that collapses too), and merged back with the library's
own alignment. Results through the shipped code path: the two affected captures
came out fully formatted, with three changed words in the 75 s one ("619" →
"6:19", "issues and execution" → "issues in execution", one repeated "just"
dropped) and none in the 588 s one; the other 18 transcripts are byte-identical.
The repair cost 1.3 s and 0.3 s on the two captures and nothing on the others.
On the 100 history transcripts the detector would have tried a repair in 20.

*Note, October 2, 2026 (after 0.6.3):* the splice described here is no longer
what the code does. A repair now replaces only the words of the unformatted
stretch, cut at the words on either side that both recognitions heard alike,
and the wording check uses an exact longest common subsequence. The author's
re-run on the archive after that change reports the repeated "just" dropped
and no other word changed: "619" → "6:19" no longer happens.

### Self-update

The updater was exercised with file URLs and a fake bundle: a newer tag is
offered, the same or older is not, a release without exactly one `.zip` and
`.zip.sha256` is reported, a corrupted archive or an app with another bundle
identifier or version is refused before anything moves. The swap script itself
was run against temporary folders (including paths with spaces and an
apostrophe, which the first version mishandled): it installs the new app,
keeps the old one, removes the download, opens the result, and puts the old app
back if the new one cannot be placed. The hand-off waits until the app has been
idle on two looks 3 s apart. The first live update (0.6.0 to 0.6.1) is recorded under 0.6.3 below.

## 0.6.1 independent audit of 0.6.0 — October 2, 2026

Two fresh code-review agents audited 0.6.0 without the author's reasoning: one the
updater, one the transcription repair and the whole change against the Clean
Code standard. All 302 tests, Ruff, and mypy pass after the fixes below.

### Updates

- **Trust (both reviewers).** 0.6.0 accepted any app that matched the release's
  own SHA-256 and carried a valid signature, which an ad-hoc signature always
  is: whoever could publish a GitHub release could replace every installed
  copy. Releases are now signed with a self-signed certificate that exists only
  in a keychain on the build machine, and the updater requires that exact
  certificate (`codesign -R`). Because 0.6.0 trusts any signature, it accepts
  0.6.1; from 0.6.1 on only the pinned certificate is accepted. The same stable
  identity lets macOS keep the microphone and Accessibility permissions across
  updates.
- **A swap across volumes could leave a half-copied app** (reproduced by the
  reviewer on a 4 MB disk image). The new app is now placed beside the
  installed one before Maramax quits, so the swap is two renames in one
  folder; every step is checked and an app without an `Info.plist` is never
  opened.
- **A failed swap was silent.** The script now leaves an outcome that the next
  launch reports.
- **The automatic prompt's default button installed.** Return now means
  Later, and the prompt comes to the front instead of opening behind windows.
  An install also waits while any dialog or file panel is open.
- Smaller: a 404 no longer reads as "up to date"; rate limiting says so; an
  asset still uploading or without a URL is reported; `0.6` equals `0.6.0`;
  the rollback copy and read-only locations are refused before downloading;
  a failed download is deleted; release notes are shown without Markdown;
  sizes under 1 MB are shown in KB; the relaunch waits a second for
  LaunchServices and ignores an instance that is exiting; `publish_release.py`
  publishes only a clean build of the pushed HEAD and never reuses a tag.

### Unformatted-stretch repair

- **The 40-word rule fired on formatted run-on sentences** (11 of the 100 history
  transcripts have one), and each firing replaced correct text with a
  different recognition. A long run now counts only if it has no capital
  letter at all; with a lower-case "i" the 12-word rule stands. A repair is
  accepted only if it keeps at least 90 % of the words. On history the
  detector now flags exactly the 8 transcripts with a lower-case "i".
- **A short last chunk could repeat words** ("AKA, aka"): when a window ended
  less than one overlap after a chunk boundary, the loop recognized a piece
  lying wholly inside the previous chunk's overlap. It now stops at the chunk
  that reaches the end, for the 120 s first pass as well.
- **A cancel during the repair discarded a finished transcript.** It now keeps
  the first pass.
- No window is recognized twice, spans are found again after each splice, and
  chunks are cut at sample indices (seconds lost a sample for about 1 % of
  lengths).

Through the shipped code path on the 22 archived dictations: the two affected
captures are fully formatted (word changes: "619" → "6:19" and one repeated
"just" in the 75 s capture, none in the 588 s one); every other transcript is
identical to what the app stored. (See the note under 0.6.0: since the repair
changes only the stretch, "619" stays as it was.)

## Hardware checks before a release

After it is convenient to use audio, test with built-in input and explicitly
selected AirPods: immediate speech after the shortcut, short and long recordings,
repeated dictations, reconnects between recordings, disconnection while
recording in Automatic mode (it should continue on the Mac's microphone), and
back-to-back dictations with the microphone kept connected. Verify the actual selected input, passive focus behavior, Cmd+R being
released afterward, optional insertion, playback, and recovery after force-quit.

Compare `open_delay` (request to device open), `first_frame_delay`,
`stop_seconds`, `stop_to_result_seconds`, `warm_start`, `audio_worker_resets`,
and `active_threads` in recording metadata across repeated sessions. Measure real stop-to-insertion latency separately; the stored
stop-to-result timing does not include the final UI/clipboard insertion.

## 0.6.3 audit of deltas, the progress window, and the docs — October 2, 2026

Two fresh code-review agents audited 0.6.2 (one the delta updates and the
progress window, one the design, the documentation, and the README); a final
review by the author followed their fixes. All 327 tests, Ruff, and mypy pass.

### Updates

- **A delta did not see directories or directory symlinks**, so a release that
  removed a Python package would have left empty folders behind (which Python
  then imports as namespace packages) in an app the strict signature check
  still accepts. Deltas now live in `bundle_delta.py`, carry the full tree of
  the new bundle (every directory, file, and symlink with its permissions), and
  the rebuilt app must equal it before the signature is even checked.
- A delta could carry quarantine from a browser-installed copy into the update;
  the rebuilt app's extended attributes are now cleared. A path through a
  symlink, or a symlink passed off as a file, is refused.
- A dictation started during the 0.8 s "Restarting Maramax…" notice would have
  been cut off by the quit. The app now looks again after the notice and goes
  back to waiting; a quit that does not happen within 15 s is reported.
- A broken delta asset blocked the whole update, and two parser errors skipped
  the fallback; any problem with a delta now falls back to the whole app.
- `create_release.py` proves each delta by rebuilding the release from the
  previous one and checking the signature, and lists exactly what
  `publish_release.py` may upload, with digests.
- The signing step's restore of the keychain search list could be skipped by an
  error at the wrong moment. It now retries, keeps a record on disk until the
  list is proved restored, refuses to run while such a record exists, and locks
  the signing keychain afterwards. (In 0.6.1 an interactive signing test by the
  author, not the build script, had written the search list back as one
  malformed entry; it was repaired the same day.)

### Transcription

- The repair checked the wording only inside the unformatted stretch, while a
  retry window can replace up to about ten seconds of formatted text before it.
  Both are now checked, separately, against what the splice actually replaces.
  *(Note, October 2, 2026: superseded. A repair now replaces nothing outside
  the stretch; see the note under 0.6.0.)*
- A cancel is its own exception, so the repair stops only for a cancel.

On the 22 archived dictations the result is unchanged from 0.6.2: the two
affected captures repaired, every other transcript identical.

### Documentation

The README leads with what the app does and its engineering problems, shows an
animation and a diagram rendered from the app's own views (Mermaid's controls
covered the diagram on GitHub), qualifies what was tested only with a fake
audio device, and credits the forked projects with a measured share.
*(Note, October 2, 2026: that share, 53 lines, could not be reproduced; the
README no longer gives a count, and the fake-device qualification now lives
only in this file.)* The
architecture has its own document, `docs/architecture.md`; the launch
guide no longer describes the author's own Mac.

### Live results

- **0.6.0 → 0.6.1**, through 0.6.0's prompt: the whole app (210 MB, about four
  minutes on this connection) downloaded, verified, swapped, and relaunched; the
  previous version kept. 0.6.1 → 0.6.2 went the same way through 0.6.1's code.
- **0.6.2 → 0.6.3**, through 0.6.2's updater code against the published release:
  the delta (1,168,147 bytes; 21 paths changed, 14 removed) downloaded and
  verified in 3.3 s, the rebuilt app matched the release and its pinned
  signature, and Maramax relaunched as 0.6.3 two seconds after quitting. Before
  publishing, the same delta was applied by 0.6.2's code and by 0.6.3's to
  clones of the installed app; both produced the release exactly.
- A bundle check run with `--audio` compiled modules into the signed bundle and
  broke its seal; `create_release.py` refused it. The check now runs without
  writing bytecode and verifies the seal when it finishes.

## 0.7.0 welcome, chosen shortcut, app icon, and a layered audit — October 2, 2026

### Audit

Twelve auditors, one per area (onboarding, update security, update flow, audio,
recognition, windows, data, build and release, design, docs, tests, and a
fuzzing pass over the pure rules), reported 84 distinct findings once
duplicates were merged. Each was checked by a verifier that tried to
reproduce it, judged whether a real user could reach it, and reviewed the
proposed fix; 73 held, none of them high severity. They were fixed in six
areas in parallel, each with tests that fail on the old code, then reviewed
together for interactions between the fixes. 456 tests pass (355 before).
Declined, with reasons in the commits: splitting `hotkeys.py`, relabelling the
bar for a shortcut changed mid-recording, and holding an install for a file
queue that was never started.

### Live results

- The build left the keychain search list exactly as it was (login, then
  openvpn) and no record file behind.
- `create_release.py` proved the 0.6.3 → 0.7.0 delta (3,912,816 bytes; 66 paths
  changed, 2 removed) by rebuilding the release from a clone of 0.6.3.
- **0.6.3 → 0.7.0**, through 0.6.3's prompt against the published release:
  the prompt appeared a minute after launch; the download was verified 40 s
  later, the user's click included (the whole app took about four minutes on
  this connection, so this was the delta); Maramax quit, and 0.7.0 was
  running 3.6 s later with its shortcut registered and its seal intact, the
  previous version kept in `updates/previous`.

## 0.8.0 word replacements, Settings, the Software Update window, and an audit — October 2, 2026

### The replacement that disappeared

`settings.json` on this Mac held two rules (`Meramax → Maramax`, `Lira →
Lyra`) while `history-originals.json` showed a third, `Kairos → Cairos`,
applied to dictations through October 2. 0.7.0's Words tab kept the last rule
it had shown (after a save or a pick from its popup) as the one "being
edited", so a new rule typed into the empty-looking form replaced it. The list
now shows every rule, a new rule is always added, and words another rule
covers are refused with that rule pointed out (`edited_rules()`, tested
against exactly this sequence).

### Audit

Four reviewers, one per area (audio, recognition and storage, updates and
packaging, windows and the controller), read their modules in full and
traced each finding end to end; a fifth reviewed the finished change set.
Nothing could install an unverified update or let a delta escape the bundle,
and no race or hang was found in the recording pipeline. Fixed, each with a
test that fails on the old code where one could be written:

- Words tab: a cell still being edited was saved into another rule's row when
  Remove, Add, or Undo changed the list; the page did not grow with its note.
- Updates: an automatic offer left behind another app stopped the daily
  check and could later install a superseded release (any check now
  withdraws a waiting offer); the app's quit watchdog (15 s) gave up before
  the swap script (60 s, which counting forked sleeps stretched to about
  68 s; it now waits by the clock); a swap that could not roll back claimed
  it had, and the copy it then opened would have deleted itself on its next
  update (refused now, by `ensure_installable` and by the script).
- Audio: a stop timeout dropped audio already in the pipe (6,400 of 22,400
  bytes kept in the test); a start truncated a crash spill that could not be
  set aside; adopting many crash leftovers pruned some of them as they
  arrived; device listing shared the recording lock, so the Microphone tab
  during a Bluetooth route change could make a dictation report "busy";
  Recover Last Recording kept choosing a silent recording.
- With pasting on, files and recoveries were not copied although Settings
  said so; a corrupt `history.json` was set aside but its originals were then
  overwritten; Qwen loaded alongside, or without, Parakeet.

474 tests pass (456 before); ruff and mypy are clean. Off-screen renders of
every Settings tab, the Software Update window, and the progress window were
checked in Light and Dark; the welcome renders pixel-identical to 0.7.0 after
its views moved to `layout.py`.

### Live results

- The build left the keychain search list exactly as it was (login, then
  openvpn); the bundle is signed by Maramax Release Signing and its seal
  verifies with the pinned requirement.
- `create_release.py` proved the 0.7.0 → 0.8.0 delta (1,248,583 bytes; 36
  paths changed, none removed) by rebuilding the release from a clone of 0.7.0.
- On this Mac 0.7.0 was replaced by hand, as the swap script does it (quit,
  old copy to `updates/previous`, new one in place), because 0.7.0's prompt
  needs a click: 0.8.0 started with Option+Space registered, loaded Parakeet
  in 2.7 s, kept the user's settings, and its first check, 57 s after launch,
  found itself the newest release. The 0.8.0 updater itself is first
  exercised live by the next release.
