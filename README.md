<h1 align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="docs/images/icon-dark.png">
    <img alt="The Maramax icon: a white M on a graphite tile" src="docs/images/icon-light.png" width="112">
  </picture>
  <br>
  Maramax
</h1>

<p align="center">On-device dictation for Apple Silicon Macs.</p>

<p align="center">
  <a href="https://github.com/maxim-golubev/maramax/releases/latest"><b>Download for Apple Silicon</b></a> ·
  <a href="docs/guide.md">User guide</a> ·
  <a href="docs/architecture.md">Architecture</a> ·
  <a href="docs/validation.md">What was measured</a>
</p>

<p align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="docs/images/dictation-dark.gif">
    <img alt="The Maramax dictation bar through one dictation: connecting, recording with a live level meter and timer, transcribing, then 'Copied transcript to clipboard'" src="docs/images/dictation-light.gif" width="460">
  </picture>
</p>

Press **Option+Space** (or a shortcut you choose), speak, press it again:
NVIDIA's Parakeet model transcribes on the GPU through MLX, and the text is
copied, or pasted into the app you were using. No audio or text leaves the Mac.

- **Fast:** on an M3 Pro, a dictation under 30 seconds is transcribed in about
  0.3 s and one over two minutes in about 3 s (medians over 20 real dictations).
- **Private:** recognition, history, and recordings stay on the Mac. The only
  network requests it makes by itself are the one-time speech model download
  and a daily update check, which can be turned off.
- **Built with:** Python 3.12, PyObjC/AppKit for the native interface, MLX for
  inference, PortAudio in a separate helper process, Carbon hotkeys through
  ctypes, py2app. About 9,400 lines of app code and 5,800 of tests; built on
  macOS 15.

## Engineering problems

A speech model is the easy part. Most of the work went into these:

- **Bluetooth audio drivers hang.** A call into the macOS audio stack can block
  forever while AirPods switch modes, so the app never makes one: a helper
  process owns the microphone, every start and stop has a deadline, and a
  helper that stops answering is replaced without losing the audio it already
  sent. In Automatic mode a microphone that disappears mid-sentence is swapped
  for the next one. (Exercised with a fake audio device driving the real
  helper; live AirPods failover is on the hardware checklist.)
- **No dictation is ever lost.** Audio is written to disk as it arrives and
  archived before recognition starts, so neither a crash nor a failed
  transcription can cost a recording.
- **The model sometimes stops punctuating.** In long dictations Parakeet can
  write a stretch in lower case with no punctuation. Experiments on real
  recordings traced it to where the encoder's audio window starts, not to the
  decoder, so Maramax finds those stretches, transcribes them again in shorter
  windows, and splices them back only if the wording still matches (at least
  90 % of the words).
- **Updates are signed and small.** An update is accepted only with Maramax's
  own release signature and downloads only the files that changed (1.2 MB for
  0.6.3, against 210 MB for the whole app). It rebuilds the app from a copy of
  the installed one, proves the result identical to the release before
  swapping it in, and keeps the previous version.

Over 450 tests run without a microphone, a screen, or model weights: a fake
audio device drives the real helper process, the native windows are built and
measured off-screen, and the update swap script runs against temporary folders.

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="docs/images/how-it-works-dark.svg">
  <img alt="Your shortcut reaches Maramax, which asks an audio helper process (it owns the microphone) to record and stop and receives the audio over a pipe; the audio is archived as a WAV first, then recognized from memory by Parakeet TDT 0.6B v2 on the GPU, word replacements are applied, and the text goes to the clipboard or is pasted" src="docs/images/how-it-works-light.svg" width="980">
</picture>

## Install

1. Download `Maramax-<version>.zip` from
   [Releases](https://github.com/maxim-golubev/maramax/releases/latest), unzip
   it, and move `Maramax.app` to Applications.
2. Open it. It is signed with the project's own certificate but not notarized
   by Apple, so the first time macOS blocks it: open **System Settings →
   Privacy & Security** and choose **Open Anyway**.
3. The first launch downloads the speech model (2.5 GB). After that Maramax
   starts in a few seconds and works offline.

Allow microphone access when asked; turn on Accessibility only if you want the
text pasted for you. Settings also has your own replacement list for names and
jargon it mishears and an optional larger recognizer (Qwen3-ASR 1.7B) that takes
that list as vocabulary; the transcript window transcribes audio and video files
in batches.

## Build from source

Requires Python 3.12 and Homebrew's PortAudio (FFmpeg only for importing media files).

```sh
brew install portaudio ffmpeg
uv sync --extra dev
./run.sh                                  # run from source
.venv/bin/python -m pytest -q             # tests
bash build_app.sh                         # dist/Maramax.app, checked before it is kept
```

## Credits

Maramax started as a fork of Osada Paranaliyanage's
[parakeet-dictation](https://github.com/osadalakmal/parakeet-dictation), itself
built on Ashwin P Chandran's
[whisper-dictation](https://github.com/ashwin-pc/whisper-dictation). It has
since been rewritten: `git blame` attributes 32 of the 9,397 lines in `src/`
to the earlier projects. Recognition uses NVIDIA's Parakeet TDT 0.6B v2 through
[parakeet-mlx](https://github.com/senstella/parakeet-mlx), and optionally
Qwen3-ASR through [qwen3-asr-mlx](https://github.com/gabrimatic/qwen3-asr-mlx).
MIT licensed.
