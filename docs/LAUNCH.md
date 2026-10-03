# Maramax — start here

## First launch

1. Move `Maramax.app` (beside this guide) into **Applications** and open it.
2. The first time, macOS blocks it: Maramax is signed with its own release
   certificate but not notarized by Apple. Open **System Settings → Privacy &
   Security** and choose **Open Anyway**. The app then appears in the menu bar.
3. A short welcome asks which shortcut you want (Option+Space is recommended)
   and whether Maramax should paste the text for you or only copy it. Meanwhile
   the first launch downloads the speech model (about 2.5 GB); after that it
   loads from the local cache in a few seconds, with no network request.
4. Press your shortcut and speak; press it again, or **Cmd+R**, to finish. The
   transcript is on your clipboard: paste it with **Cmd+V**.

macOS asks for microphone access the first time you dictate; allow it. If you
denied it earlier, switch Maramax on under **System Settings → Privacy &
Security → Microphone**. For the transcript to be pasted for you, turn on
**Settings → Paste into the active app**; Maramax opens **System Settings →
Privacy & Security → Accessibility**, where you switch Maramax on. Opening the
app does not open the microphone; only dictating does.

If an older Maramax is running, quit it from its menu bar icon first: one copy
runs at a time. Settings, history, and recordings are shared between versions.

## Updates

Maramax keeps itself up to date. Once a day it asks GitHub whether a newer
version has been published and offers it: **Install and Relaunch**, **Later**,
or **Skip This Version** (Return means Later, so a keystroke meant for another
app never installs anything). **Check for Updates…** in the menu, or **Check
Now** in **Settings → General**, asks right away; the top of Settings shows the
version you have.

**Install and Relaunch** shows the download in a small window (only the files
that changed, when possible), accepts it only if it carries Maramax's release
signature, waits until Maramax is idle (not dictating, transcribing, or saving
audio), and restarts it as the new version. Settings, history, and recordings
are untouched; if anything goes wrong, the next launch says so. The version it
replaced is kept at
`~/Library/Application Support/Maramax/updates/previous/Maramax.app`: to roll
back, quit Maramax and move that copy into Applications. Because every release
carries the same signature, macOS keeps the microphone and Accessibility
permissions across updates. The daily check can be turned off in **Settings →
General → Check for updates automatically**.

## Everyday use

- **The bar** stays out of the way and leaves the app you are typing in
  focused. Its meter shows incoming audio and its timer the captured length.
  The red button finishes; the arrow opens the full window.
- **Open Transcript** shows the transcript, history, and a queue for
  transcribing audio and video files.
- **Settings…** has copy and paste, live preview, the microphone, the optional
  larger recognizer, and word replacements: a phrase it keeps mishearing and
  the spelling you want. The original wording stays in history and recordings.
- **Recordings…** plays, exports, or transcribes again any saved capture,
  including ones that returned no transcript.

Automatic input prefers the Mac's own microphone (except with the lid closed),
so AirPods stay in high-quality playback; choose a microphone in **Settings →
Microphone** to always use it. AirPods need two to three seconds to connect
before they deliver sound: start speaking when the bar says **Recording**.
**Keep the microphone connected for** keeps that connection open between
dictations so the next one starts at once; while it is open, macOS shows the
microphone indicator and AirPods play in call quality.

If the microphone disconnects in Automatic mode, the dictation continues on the
next available input and says it switched; a microphone you chose yourself is
never swapped, and the dictation ends with what was captured. If you switch
apps while dictating in the bar, or the clipboard changes before pasting,
nothing is pasted and the transcript stays in history.

## When something goes wrong

- **The speech model does not load:** check the connection, then **More →
  Retry Speech Model**.
- **A dictation returned nothing:** the audio is still in **Recordings…**; play
  it first. A silent recording needs a working microphone, not another try.
- **Maramax quit or crashed while recording:** the audio appears in
  **Recordings…** at the next launch.

## Local data

Everything lives in `~/Library/Application Support/Maramax/`: settings,
transcripts, recordings (ordinary WAV files with JSON details, up to 20
recordings or 512 MB, always keeping the newest), and logs (2 MB, two
backups; transcript text is not logged). **More → Clear History &
Recordings…** deletes transcripts and audio. Speech models stay in the Hugging
Face cache. Importing media files needs FFmpeg (`brew install ffmpeg`);
dictation does not.
