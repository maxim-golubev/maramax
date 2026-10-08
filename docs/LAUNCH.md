# Maramax — start here

## First launch

Maramax needs an Apple Silicon Mac with macOS 15 or later.

1. Move `Maramax.app` (beside this guide) into **Applications** and open it.
2. The first time, macOS blocks it: Maramax is signed with its own release
   certificate but not notarized by Apple. Open **System Settings → Privacy &
   Security** and choose **Open Anyway**. The app then appears in the menu bar.
3. A short welcome asks which shortcut you want (Option+Space is recommended)
   and whether Maramax should paste the text for you or only copy it, then
   shows what a dictation looks like on the bar. Meanwhile
   the first launch downloads the speech model (about 2.5 GB); after that it
   loads from the local cache in a few seconds, with no network request.
4. Press your shortcut and speak; press it again, or **Cmd+R**, to finish. The
   transcript is on your clipboard: paste it with **Cmd+V**.

macOS asks for microphone access the first time you dictate; allow it. If you
denied it earlier, switch Maramax on under **System Settings → Privacy &
Security → Microphone**. For the transcript to be pasted for you, choose **Paste
into the app you’re using** (in the welcome, or **Settings → General**): macOS
asks once, and its button opens **Privacy & Security → Accessibility**, where
you switch Maramax on. Opening the app does not open the microphone; only
dictating does.

If an older Maramax is running, quit it from its menu bar icon first: one copy
runs at a time. Settings, history, and recordings are shared between versions.

## Updates

Maramax keeps itself up to date. Once a day it asks GitHub whether a newer
version has been published and offers it in a **Software Update** window with
the release notes: **Install Update**, **Remind Me Later**, or **Skip This
Version**. A daily offer appears without taking the keyboard, so typing meant
for another app never reaches it. **Check for Updates…** in the menu, or **Check
Now** in **Settings → Advanced**, asks right away; the top of Settings shows the
version you have.

**Install Update** shows the download in a small window (only the files
that changed, when possible), accepts it only if it carries Maramax's release
signature, waits until Maramax is idle (not dictating, transcribing, or saving
audio), and restarts it as the new version. Settings, history, and recordings
are untouched; if anything goes wrong, the next launch says so. The version it
replaced is kept at
`~/Library/Application Support/Maramax/updates/previous/Maramax.app`: to roll
back, quit Maramax and move that copy into Applications. Because every release
carries the same signature, macOS keeps the microphone permission across
updates, and the Accessibility permission once it was granted to a release; one
left from a build before 0.6.1 is not kept, and choosing **Allow…** in Settings
replaces it. The daily check can be turned off in **Settings → Advanced → Check
for updates automatically**.

## Everyday use

- **The bar** stays out of the way and leaves the app you are typing in
  focused. Its meter shows incoming audio and its timer the captured length.
  The red button finishes; the arrow opens the full window. Drag the bar
  anywhere you like: it opens where you leave it, and a double-click puts
  it back.
- **Open Transcript** shows the transcript, history, and a queue for
  transcribing audio and video files. To transcribe a file, drop it on the
  Maramax icon in the menu bar, or choose **Transcribe Files…**.
- **Settings…** has where the transcript goes (paste, copy, or keep), live
  preview, the microphone, word replacements (a phrase it keeps mishearing and
  the spelling you want; the original wording stays in history and
  recordings), the optional high-accuracy model, and updates.
- **Recordings…** plays, exports, or transcribes again any saved capture,
  including ones that returned no transcript.

Automatic input prefers the Mac's own microphone (except with the lid closed),
so AirPods stay in high-quality playback; choose a microphone in **Settings →
Microphone** to always use it. AirPods need two to three seconds to connect
before they deliver sound: while the bar says **Don’t speak yet** in orange,
wait; start speaking when it says **Recording**. **Keep the microphone
connected** keeps that connection open between
dictations so the next one starts at once; while it is open, macOS shows the
microphone indicator and AirPods play in call quality.

If the microphone disconnects in Automatic mode, the dictation continues on the
next available input and says it switched; a microphone you chose yourself is
never swapped, and the dictation ends with what was captured. If you switch
apps while dictating in the bar, or the clipboard changes before pasting,
nothing is pasted and the transcript stays in history.

## When something goes wrong

- **The speech model does not load:** check the connection, then press your
  shortcut again or choose **Retry Speech Model** at the top of the menu.
- **Pasting stopped working:** choose **Allow…** under **Settings → General →
  Paste into the app you’re using**; in the dialog macOS shows, choose **Open
  System Settings** and switch Maramax on under **Privacy & Security →
  Accessibility**.
- **The bar says the microphone sent only silence, every time:** switch
  Maramax on under **System Settings → Privacy & Security → Microphone**, then
  check the input under **Settings → Microphone**.
- **A dictation returned nothing:** the audio is still in **Recordings…**; play
  it first. A silent recording needs a working microphone, not another try.
- **Maramax quit or crashed while recording:** the audio appears in
  **Recordings…** at the next launch.

## Local data

Everything lives in `~/Library/Application Support/Maramax/`: settings,
transcripts (the last 100 by default), recordings (ordinary WAV files with
JSON details; the last 20 by default, under 512 MB in all, always keeping the
newest; both numbers are chosen in **Settings → Advanced**), and logs (2 MB, two
backups; transcript text is not logged). **Settings → Advanced → Clear History
& Recordings…** deletes transcripts and audio. Speech models stay in the Hugging
Face cache. Importing media files needs FFmpeg (`brew install ffmpeg`);
dictation does not.
