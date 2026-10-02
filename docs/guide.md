# Using Maramax

Everything the app does and where it keeps things. For a first launch, see [the launch guide](LAUNCH.md).

## Everyday dictation

- Press **Option+Space** to start, then **Option+Space** or **Cmd+R** to finish.
- A small bar shows the selected microphone, captured duration, and actual input
  level without taking focus from your current app. Cmd+R is registered globally
  only while recording from the compact bar; it is released afterward.
- Results copy to the clipboard by default; turning **Copy the transcript to the
  clipboard** off is honoured however a dictation is finished. **Settings → Paste
  into the active app** enables insertion too, into the app you were last working
  in. In compact mode, insertion is skipped if you switch to a different app while
  dictating; the text stays copied. If the clipboard changes before insertion, or
  you cancel while it is transcribing, nothing is pasted and the transcript
  remains in history. Expanding the bar during an operation preserves its
  original destination app.
- Use the arrow in the bar or **Open Transcript** for the full transcript, history,
  and file queue. Live transcription previews remain available in that window.
- Disable **Use the compact dictation bar** to use the original window interaction.
- Capture continues for a fifth of a second after you press stop, so a last
  syllable still travelling through the driver or a Bluetooth link is not cut off.

**Settings…** opens native controls for these preferences and your word
replacements. Enter a phrase the recognizer gets wrong and its desired spelling.
Replacements match whole words or phrases without case sensitivity and apply
once, preferring longer phrases. They apply to dictation and recording retries;
the original transcript is retained in history and recording details. Imported
media is transcribed without these replacements. No additional language model
rewrites the text in this release. Standard editing shortcuts (Cmd+V, Cmd+C,
Cmd+A, Cmd+Z, Cmd+W) work in Settings and the other windows.

With **Use the high-accuracy model** on, the names and terms you entered as
replacements are also given to that model as vocabulary before it listens
(snippets, addresses, and lists are left out). On this Mac it is several times
slower than the standard model (a five-minute dictation took about a minute
instead of five seconds), so it stays off by default.

If the speech model cannot load, use **Retry Speech Model** after restoring your
connection. **Quick Start…** explains recording, insertion, microphone selection,
and recovery. A second copy of Maramax is blocked before it loads models or opens
audio; quit the old copy before opening a different version.

## Microphones and AirPods

Automatic mode prefers the Mac's built-in microphone when available, allowing
headphones to remain an output device. It does not change the system input or
output setting. Choose a specific microphone in **Settings → Microphone** to
override automatic selection; that choice persists, and the Automatic entry names
the microphone it would use right now. Disable **Prefer the Mac's own microphone
in Automatic** to follow the system default instead. With the lid closed the
built-in microphone is switched off in hardware, so the preference is skipped.

Maramax does not initialize PortAudio or open input at launch. Once the speech
model is ready it starts its audio helper process on standby, without touching
any device, so a recording pays only for the driver open rather than a process
launch (about 125 ms saved per dictation on this Mac). Microphone startup and
teardown run off the main UI thread. The bar distinguishes waiting for input,
digital silence, and a stream that stopped delivering audio. Ordinary pauses
after signal has arrived do not count as disconnections.

If the microphone disappears or stops delivering audio mid-dictation, Automatic
mode reopens whichever input macOS now offers and continues the same recording;
the result notes that the microphone changed. A microphone you selected
explicitly is never swapped: the recording ends at once and what was captured is
kept. If the audio driver itself gets stuck, the helper process is replaced, so
the next dictation starts from a clean state.

Bluetooth microphones deliver one and a half to two and a half seconds of
silence each time they connect; that is the headset switching into call mode and
no app can shorten it. **Keep the microphone connected for** (Off, 30 seconds,
2 minutes, 5 minutes) leaves the stream open after a dictation so the next one
starts instantly. While it is open macOS shows the microphone indicator and
AirPods stay in call-quality playback; audio heard while waiting is discarded
inside the helper and never reaches the app. It is off by default, a change
takes effect immediately (even for a dictation in progress), and changing the
microphone closes a connection that was being kept open.

Hardware behavior needs validation on the specific headset and macOS version;
the tests use a simulated audio device and cannot establish that a Bluetooth
problem is fixed.

## Recordings and recovery

**Recordings…** opens saved audio with playback, WAV export, and
**Transcribe Again**. Audio is archived before recognition, including captures
that produce no transcript. An empty recognizer result never deletes that audio,
and neither does choosing **Clear History & Recordings…** while a dictation is
still running behind its confirmation.

Recordings are ordinary local WAV files with JSON metadata under
`~/Library/Application Support/Maramax/recordings`. Metadata includes microphone
identity, input measurements, outcome, transcript, and timing. No extra encryption
is applied by Maramax. The archive retains up to **20 recordings / 512 MB**, keeping
the newest even if it alone exceeds that budget; older recordings are removed
as new ones are saved. Export recordings you want to keep permanently.

The live PCM recovery spill is still written during capture for crash recovery.
At the next launch a leftover spill is moved into Recordings as an ordinary
entry. **Recover Last Recording** retries audio that never reached the recognizer
first, then the newest capture without a transcript.
**Clear History & Recordings…** deletes both transcript history and retained audio
after confirmation, and is unavailable during an active operation.

The original transcript for a replaced phrase is stored separately in
`history-originals.json`, keeping `history.json` readable by 0.3.0. Both are
cleared by the app's history command. Operational logs rotate at 2 MB with
two backups under `logs/`; no transcript text is deliberately logged.

Settings, history, and recording details written by a newer version keep their
extra fields when an older version saves them, so rolling back does not erase
anything. A settings or history file that cannot be read is renamed to
`*.corrupt` instead of being overwritten.

## Updates

Maramax checks GitHub for a newer release once a day (turn it off in
**Settings → General**); **Check for Updates…** in the menu or **Check Now** in
Settings asks right away, and the top of Settings shows your version. Choosing
**Install and Relaunch** opens a small window with the download's progress;
Maramax then restarts by itself (after you finish dictating, if you are). An
update downloads only the files that changed since your version when it can,
and is accepted only if it carries Maramax's own release signature. The
replaced version is kept for rollback under
`~/Library/Application Support/Maramax/updates/previous`. The check sends
nothing but the request and the installed version number.
