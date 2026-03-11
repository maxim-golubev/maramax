# Maramax

On-device speech-to-text for macOS. Lives in your menu bar, transcribes with a single hotkey, and never sends audio off your machine.

Built with [NVIDIA Parakeet](https://github.com/nvidia/parakeet-mlx) running locally through MLX on Apple Silicon.

---

## What it does

Press **Option+Space** anywhere on your Mac. A floating overlay appears, recording starts, and when you stop it the transcript lands in your clipboard. That's the whole workflow.

It also transcribes audio and video files — drag them onto the overlay or use the file picker. Everything stays local, everything goes to history.

### At a glance

- **Menu bar app** with a native floating overlay (not Electron, not a web view)
- **Push-to-talk dictation** with global hotkey and auto-copy
- **File transcription** for audio/video via drag-and-drop, with chunked processing and progress
- **Input device selection** — picks your active mic by default, or choose manually
- **Transcription history** stored locally
- **Cancellable** — cancel any transcription mid-flight from the overlay
- **Fully local** — no network calls, no accounts, no telemetry

---

## Requirements

- macOS 12+ on Apple Silicon (Intel Macs will work but slowly)
- Python 3.10+
- PortAudio and FFmpeg:
  ```bash
  brew install portaudio ffmpeg
  ```

---

## Install

```bash
git clone https://github.com/maxim-golubev/maramax.git
cd maramax
uv venv -p 3.12
uv sync
```

### Run from source

```bash
./run.sh
```

### Build a standalone .app

```bash
uv sync --extra dev
bash build_app.sh
# Output: dist/Maramax.app — drag to /Applications
```

The first launch downloads the Parakeet model (~400 MB) to your local cache. After that it starts in seconds.

---

## Usage

| Action | Shortcut |
|---|---|
| Open overlay | **Option+Space** |
| Start/stop recording | **Cmd+R** |
| Copy transcript | **Cmd+C** |
| Close overlay | **Esc** |

**Dictation**: Option+Space opens the overlay and starts recording automatically. Cmd+R stops recording and triggers transcription. The result is copied to your clipboard.

**File transcription**: Drag audio or video files onto the overlay, or click **Files...** to use the picker. Supports aac, aiff, flac, m4a, mov, mp3, mp4, ogg, opus, wav, and webm.

**Menu bar**: Right-click the menu bar icon for quick access to overlay, history, recording controls, and file import.

---

## Permissions

macOS will prompt for these on first use:

- **Microphone** — for recording audio
- **Accessibility** — for the global hotkey (Option+Space)

Grant them in System Settings > Privacy & Security.

---

## How it works

The app is a Python menu bar app ([rumps](https://github.com/jaredks/rumps)) with a native AppKit overlay ([PyObjC](https://pyobjc.readthedocs.io/)). Audio capture uses PyAudio, global hotkeys use the Carbon API, and transcription runs through [parakeet-mlx](https://github.com/nvidia/parakeet-mlx) — NVIDIA's Parakeet ASR model compiled for Apple Silicon via [MLX](https://github.com/ml-explore/mlx).

Long files are transcribed in chunks with overlap to avoid cutting words at boundaries. Transcription runs in a background thread so the UI stays responsive, and any job can be cancelled from the overlay.

The standalone .app is built with py2app. MLX is a namespace package with C extensions, which required some non-trivial packaging workarounds — see `build_app.sh` and `packaging/setup.py` if you're curious.

---

## Project structure

```
src/parakeet_dictation/
  app.py            # Core controller, owns all state and coordinates components
  overlay.py        # Native NSPanel overlay UI
  transcription.py  # Audio recording and Parakeet transcription
  hotkeys.py        # Global hotkey registration via Carbon API
  history.py        # Local transcript history store
  clipboard.py      # Clipboard integration
  config.py         # App configuration
  paths.py          # Resource path resolution
  main.py           # Entry point
packaging/
  setup.py          # py2app configuration
  maramax_app.py    # Bundle entry point
build_app.sh        # Build + package script
```

---

## Troubleshooting

- **No audio captured**: Make sure `portaudio` is installed before Python deps. Check Microphone permission.
- **File transcription fails**: Install `ffmpeg` (`brew install ffmpeg`).
- **Hotkey doesn't work**: Grant Accessibility permission. Restart the app after granting.
- **Slow first transcription**: The model warms up on first use. Subsequent transcriptions are fast.

---

## Credits

- [parakeet-mlx](https://github.com/nvidia/parakeet-mlx) — NVIDIA Parakeet ASR on Apple Silicon
- [MLX](https://github.com/ml-explore/mlx) — Apple's ML framework for Apple Silicon
- Originally forked from [parakeet-dictation](https://github.com/osadalakmal/parakeet-dictation)

## License

MIT
