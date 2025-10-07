# Parakeet Dictation (macOS)

Local, fast, privacy-friendly dictation for macOS using NVIDIA Parakeet (MLX on Apple Silicon) with a push-to-talk hotkey.  
Bonus: speak commands to **rewrite selected text** via AWS Bedrock (Claude).

---

## Table of Contents

- [Why this project?](#why-this-project)
- [Features](#features)
- [Demo](#demo)
- [Requirements](#requirements)
- [Installation](#installation)
- [Configuration](#configuration)
- [Usage](#usage)
  - [Push-to-talk dictation](#push-to-talk-dictation)
  - [Voice-driven text editing (Claude via Bedrock)](#voice-driven-text-editing-claude-via-bedrock)
  - [Menu bar controls](#menu-bar-controls)
- [Permissions (macOS)](#permissions-macos)
- [Run in the background](#run-in-the-background)
- [Troubleshooting](#troubleshooting)
- [Development](#development)
- [Roadmap](#roadmap)
- [FAQ](#faq)
- [Credits](#credits)
- [License](#license)

---

## Why this project?

Parakeet Dictation gives you **on-device** speech-to-text on macOS with a **single push-to-talk key**. It’s built to be:

- **Private**: Audio is processed locally on your Mac.
- **Fast**: Parakeet models are optimized and run great on Apple Silicon via MLX.
- **Practical**: Dictate into *any* app, or select text and **say how to transform it** (“make this more professional”, “translate to Spanish”, etc.)-the app rewrites it via AWS Bedrock and pastes it in place.

---

## Features

- 🖥️ **Menu bar** app (stays out of your way)
- 🎙️ **Push-to-talk**: Press the **Globe / Fn** key to start recording, press again to transcribe & paste
- ⚡ **Local ASR** with **NVIDIA Parakeet** (Apple Silicon via MLX)
- ⌨️ **Auto-paste at cursor** in the foreground app
- ✨ **Voice-driven text editing** (optional): when text is selected, your speech is treated as an instruction and the selection is replaced with the result (via AWS Bedrock → Claude)
- ✅ Clear **recording status** via the menu bar icon
- 🧰 Simple **background mode** (no UI) for power users

---

## Demo

TBD

---

## Requirements

- **macOS 12+** (Apple Silicon recommended for speed)
- **Python 3.10+**
- **Microphone**
- **Accessibility permission** (to paste text programmatically)
- **PortAudio** (for PyAudio)

---

## Installation

### 1. Install system deps

```bash
brew install portaudio ffmpeg
```

### 2. Clone & install Python deps

```bash
git clone https://github.com/osadalakmal/parakeet-dictation.git
cd parakeet-dictation

uv venv -p 3.12.5
source .venv/bin/activate
uv add -r requirements.txt
```

---

## Usage

### Push-to-talk dictation

1. Launch the app (see Development or Background sections below).
2. Press the **Ctrl+Alt+A** (🌐) / Function key to start recording.
3. Speak normally.
4. Release the keys again to stop. The app will transcribe and paste the text at your current cursor position.

### Voice-driven text editing (Qwen via MLX)

1. Select text in any app.
2. Press the Globe / Fn key and speak an instruction, e.g.:
  - “Fix the grammar”
  - “Summarize this”
  - “Translate to Spanish”
3. Press the key again to stop. The selected text will be replaced with the edited version.

When no text is selected, your speech is treated as dictation and the text is inserted normally.

### Menu bar controls

- Start/Stop Listening - toggles recording
- Settings - (future) configuration UI
- Quit - exits the app

---

## Permissions (macOS)

- **Microphone**: System Settings → Privacy & Security → Microphone → allow your Terminal/app
- **Accessibility**: System Settings → Privacy & Security → Accessibility → allow your Terminal/app

Without Accessibility permission, the app cannot paste text for you.

---

## Run in the background

```bash
nohup PYTHONPATH=src python -m parakeet_dictation.main >/dev/null 2>&1 & disown
```

Stop it later:

```bash
ps aux | grep 'parakeet_dictation'
kill -9 <PID>
```

---

## Troubleshooting

- No audio: ensure `portaudio` is installed
- Nothing pastes: check Accessibility permissions
- High CPU usage: first run warms up the model

---

## Development

```bash
source .venv/bin/activate
PYTHONPATH=src PARAKEET_LOG=debug python -m parakeet_dictation.main
```

- Menu bar UI via `rumps`
- Hotkey via `pynput`
- Audio capture via `pyaudio`
- ASR via `parakeet-mlx`
- AI powered edits via Qwen

---

## Roadmap

- Preferences UI (Status bar is pretty finicky)
- Streaming/partial results (It seems nice to start pasting things soon as we can)
- macOS app packaging (Non techie folks can use this as well)
- Latency/quality settings

---

## FAQ

**Does dictation send audio to the cloud?** No. Local only.
**What languages are supported?** English. That's the only one I know :). Feel free to try other languages parkeet supports.

---

## Credits

- Parakeet MLX (NVIDIA Parakeet on Apple Silicon)
- Originally forked from a [Whisper-based dictation app](https://github.com/ashwin-pc/whisper-dictation)

---

## License

MIT
