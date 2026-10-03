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
    <img alt="The Maramax dictation bar through one dictation: 'Don’t speak yet' in orange while the microphone connects, recording with a live level meter and timer, transcribing, then 'Copied transcript to clipboard'" src="docs/images/dictation-light.gif" width="460">
  </picture>
</p>

Press **Option+Space** (or a shortcut you choose), speak, and press it again.
NVIDIA's Parakeet model transcribes on the GPU through MLX, and the text is
copied, or pasted into the app you were using. No audio or text leaves the Mac.

- **Fast:** on an M3 Pro, a dictation under 30 seconds is transcribed in about
  0.25 s, and one over two minutes in about 2.3 s (medians over 20 real
  dictations).
- **Private:** recognition, history, and recordings stay on the Mac. The only
  network requests it makes by itself are the one-time speech model download
  and a daily update check, which can be turned off.
- **Built with:** Python 3.12, PyObjC/AppKit for the native interface, MLX for
  inference, PortAudio in a separate helper process, Carbon hotkeys through
  ctypes, py2app.

## Engineering

The speech model was the easy part. Most of the work went into four problems:

- **Bluetooth audio drivers hang.** A call into the macOS audio stack can block
  forever while AirPods switch modes, so the app never makes one itself. A
  helper process owns the microphone, every start and stop has a deadline, and
  a helper that stops answering is replaced without losing the audio it already
  sent. In Automatic mode, a microphone that drops out mid-sentence is replaced
  by the next available one and the recording continues. AirPods also send one
  and a half to two and a half seconds of pure silence while they connect, so
  the bar says "Don't speak yet" until real sound arrives.
- **No dictation is lost.** Audio is written to disk as it arrives and archived
  before recognition starts, so neither a crash nor a failed transcription can
  cost a recording.
- **The model sometimes stops punctuating.** In long dictations, Parakeet can
  write a whole stretch in lower case with no punctuation. Experiments on real
  recordings traced this to where the encoder's audio window starts, not to the
  decoder. Maramax detects those stretches, transcribes them again in shorter
  windows, and replaces only the affected text, and only if at least 90% of
  the words still match.
- **Every change to the text is measured first.** Candidate fixes are replayed
  against archived dictations, and a change ships only if everything it was not
  meant to touch comes out byte for byte the same. That is how clock times
  ("8.45 p.m." becomes "8:45 p.m.") and filler removal got in, and how silence
  trimming, beam search, and collapsing repeated words were kept out: each made
  real transcripts worse.

About 600 tests run without a microphone, a screen, or model weights: a fake
audio device drives the real helper process, and the native windows are built
and measured off-screen.

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="docs/images/how-it-works-dark.svg">
  <img alt="Your shortcut reaches Maramax, which asks an audio helper process (it owns the microphone) to record and stop and receives the audio over a pipe; the audio is archived as a WAV first, then recognized from memory by Parakeet TDT 0.6B v2 on the GPU, word replacements are applied, and the text goes to the clipboard or is pasted" src="docs/images/how-it-works-light.svg" width="980">
</picture>

## Install

Requires an Apple Silicon Mac; tested on macOS 15.

1. Download `Maramax-<version>.zip` from
   [Releases](https://github.com/maxim-golubev/maramax/releases/latest), unzip
   it, and move `Maramax.app` to Applications.
2. Open it. The app is signed with the project's own certificate but not
   notarized by Apple, so macOS blocks the first launch: open **System Settings
   → Privacy & Security** and choose **Open Anyway**.
3. The first launch downloads the speech model (2.5 GB). After that Maramax
   starts in a few seconds and works offline.

Also in the app: a replacement list for names and jargon it mishears, an
optional high-accuracy model (Qwen3-ASR 1.7B) that takes that list as
vocabulary, and batch transcription of audio and video files.

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
since been almost entirely rewritten. Recognition uses NVIDIA's Parakeet TDT 0.6B v2 through
[parakeet-mlx](https://github.com/senstella/parakeet-mlx), and optionally
Qwen3-ASR through [qwen3-asr-mlx](https://github.com/gabrimatic/qwen3-asr-mlx).
MIT licensed.
