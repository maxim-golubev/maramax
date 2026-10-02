# Maramax 0.5.0

Local dictation for macOS on Apple Silicon. Parakeet recognizes speech on your
Mac; an optional Qwen model provides an alternative final pass. Model weights
download on first use. A complete cached standard model loads directly from disk
without a network freshness check. Audio is not uploaded for recognition.

## Everyday dictation

- Press **Option+Space** to start, then **Option+Space** or **Cmd+R** to finish.
- A small bar shows the selected microphone, captured duration, and actual input
  level without taking focus from your current app. Cmd+R is registered globally
  only while recording from the compact bar; it is released afterward.
- Results copy to the clipboard by default. **Settings → Paste into the active app**
  enables insertion too. In compact mode, insertion is skipped if you switch to
  a different app while dictating; the text stays copied. If the clipboard changes
  before insertion, auto-paste is skipped and the transcript remains in history.
  Expanding the bar during an operation preserves its original destination app.
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

With **Use the high-accuracy model** on, the spellings you entered as
replacements are also given to that model as vocabulary before it listens. On
this Mac it is several times slower than the standard model (a five-minute
dictation took about a minute instead of five seconds), so it stays off by default.

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
explicitly is never swapped: the recording ends and what was captured is kept.

Bluetooth microphones deliver one and a half to two and a half seconds of
silence each time they connect; that is the headset switching into call mode and
no app can shorten it. **Keep the microphone connected for** (Off, 30 seconds,
2 minutes, 5 minutes) leaves the stream open after a dictation so the next one
starts instantly. While it is open macOS shows the microphone indicator and
AirPods stay in call-quality playback; audio heard while waiting is discarded
inside the helper and never reaches the app. It is off by default.

Bluetooth driver failures can still require an app restart. After two abandoned
driver sessions, further recording attempts request a restart rather than allowing
resources to accumulate indefinitely. Hardware behavior needs validation on the
specific headset and macOS version; unit tests cannot establish that it is fixed.

## Recordings and recovery

**Recordings…** opens saved audio with playback, WAV export, and
**Transcribe Again**. Audio is archived before recognition, including captures
that produce no transcript. An empty recognizer result never deletes that audio.

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

## Development

Requires Python 3.12, macOS, Apple Silicon, and system PortAudio/FFmpeg:

```sh
brew install portaudio ffmpeg
uv sync --extra dev
./run.sh
```

The full app opens the microphone only on a recording request, but starting it
loads the speech model. Tests use synthetic PCM and a fake audio backend, without
opening microphones, playing sound, or loading model weights:

```sh
.venv/bin/python -m pytest -q
.venv/bin/python -m ruff check src/ tests/
.venv/bin/python -m mypy src/
```

Build the standalone app with `bash build_app.sh`. The build produces
`dist/Maramax.app`; it does not install or launch the dictation app automatically.
The build finishes by checking isolated bundled imports, certificates, a hidden
passive panel, and temporary recording storage. This check opens no audio devices.

After validation, `.venv/bin/python packaging/create_release.py` creates a
versioned app folder, launch guide, source hashes, ZIP, and checksum under
`releases/`, preserving previous releases. See [the launch guide](docs/LAUNCH.md).

To repeat the bundle check, optionally measuring recognition against a local
audio file using already cached Parakeet weights:

```sh
.venv/bin/python packaging/check_bundle.py
.venv/bin/python packaging/check_bundle.py --audio /absolute/path/speech.wav --repeats 10 --output /tmp/maramax-check.json
```

The optional recognition check never plays the file or downloads model weights.
It measures PCM-to-transcript latency, model memory, Python threads, and process
peak resident memory. It does not measure microphone startup or text insertion.
See [the validation report](docs/validation.md) for measured results and remaining
hardware checks.

Key modules: `app.py` coordinates operations; `recorder.py` owns device capture;
`capture.py` measures input health; `transcription.py` owns recognition;
`indicator.py` renders the passive bar; `overlay.py` provides the full window;
`recordings.py` and `recordings_window.py` provide saved audio and recovery.

## Hardware validation before release

After it is convenient to use audio, test with built-in input and explicitly
selected AirPods: immediate speech after the shortcut, short and long recordings,
repeated dictations, reconnects between recordings, disconnection while
recording in Automatic mode (it should continue on the Mac's microphone), and
back-to-back dictations with the microphone kept connected. Verify the actual selected input, passive focus behavior, Cmd+R being
released afterward, optional insertion, playback, and recovery after force-quit.

Compare `open_delay` (request to device open), `first_frame_delay`,
`stop_seconds`, `stop_to_result_seconds`, `warm_start`, `abandoned_audio_sessions`,
and `active_threads` in recording metadata across repeated sessions. Measure real stop-to-insertion latency separately; the stored
stop-to-result timing does not include the final UI/clipboard insertion.
