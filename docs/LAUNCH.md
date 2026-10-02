# Maramax 0.6.2 — ready for your first launch

## Open the new version

1. Quit the currently running Maramax using its menu bar icon.
2. Open `Maramax.app` beside this guide. The app appears in the menu bar.
3. Wait for the speech model to be ready, then press **Option+Space** to start
   dictating. Press **Option+Space** again, or **Cmd+R**, to finish.
4. Paste the copied transcript with **Cmd+V**. For automatic insertion, enable
   **Paste into the active app** in **Settings…** and grant the macOS Accessibility
   permission when requested.

macOS requests microphone access when recording is first attempted. Allow it for
dictation. If access was previously denied, enable Maramax under **System Settings
→ Privacy & Security → Microphone**. Opening the app alone does not initialize an
audio device or start recording.

This release uses the same local settings and history. Before replacing an existing
installation, keep a rollback copy of the previous app. To roll back, quit Maramax
and open that copy. Run one version at a time. Both versions can read the shared transcript
history; the old app does not provide the new saved-recording browser. Avoid
changing settings in the old version during rollback, as it may discard newer
preferences it does not recognize.

## Updates

Maramax updates itself. Once a day it asks GitHub whether a newer version has
been published; when there is one, it offers it with **Install and Relaunch**,
**Later**, or **Skip This Version** (Return means Later, so a keystroke meant
for another app never installs anything). **Check for Updates…** in the menu,
or **Check Now** in Settings → General, asks right away; the top of Settings
shows the version you have. **Install and Relaunch** shows the download's
progress in a small window (only the files that changed are downloaded when
possible), accepts the update only if it is signed with Maramax's own release
certificate, waits until you are not dictating, and restarts Maramax as the
new version. Settings,
history, and recordings are untouched, and if anything goes wrong the next
launch says so. The version it replaced is kept at
`~/Library/Application Support/Maramax/updates/previous/Maramax.app`; to roll
back, quit Maramax and move that copy into Applications. Turn the daily check
off in **Settings → General → Check for updates automatically**.

Builds from 0.6.1 on share one signing identity, so macOS keeps Maramax's
microphone and Accessibility permissions across updates. Coming from 0.6.0 it
asks once: allow the microphone again, and switch Maramax on again under
**System Settings → Privacy & Security → Accessibility** if you use
**Paste into the active app**.

## Everyday use

- **Compact bar:** stays out of the way and leaves your current app focused.
  Its meter reflects incoming audio, and its timer shows captured duration.
  The red button finishes; the arrow opens the full window.
- **Open Transcript:** opens the larger transcript, history,
  and file queue. The arrow in the compact bar opens the same controls.
- **Settings…:** controls automatic copy/paste, preview, input preference,
  recognition model, and word replacements. Add a heard phrase and its desired
  replacement; edit or remove it at any time. Original text stays available in
  history and saved recordings.
- **Recordings…:** play, export, or retry saved captures, including
  recordings that returned no transcript. A retry copies its result without
  inserting it into another app.

Automatic input prefers the Mac microphone unless you turn that preference off
or the lid is closed. To always use AirPods, choose them in Settings → Microphone.
Using a Bluetooth microphone changes headphone playback quality while it is open.

AirPods need two to three seconds to connect before they deliver sound; wait for
the bar to say **Recording** before speaking. If you dictate several times in a
row, **Settings → Microphone → Keep the microphone connected for** keeps the
connection open between dictations so the next one starts instantly. During that
time macOS shows the microphone indicator and AirPods stay in call-quality
playback, which is why it is off until you choose a duration.

If a microphone disconnects while you dictate, Automatic continues on the next
available input and tells you that it switched. A microphone you chose yourself
is never swapped; the dictation ends there with what was captured.

If you switch apps during compact dictation, automatic insertion is skipped and
the transcript stays copied. If the clipboard changes before insertion, Maramax
keeps the transcript in history instead of pasting the changed clipboard.

## Loading and recovery

The standard model loads directly from your existing cache on this Mac, without
a network freshness check. On a new Mac,
weights download on first use. Recognition runs locally once weights are ready.
If loading fails, check your connection and use **Retry Speech Model**. The
optional high-accuracy engine downloads approximately 4.1 GB of additional
weights. It is off by default and falls back to the standard engine on failure.

A recording interrupted by a crash or a forced quit appears in **Recordings…**
at the next launch.

If a microphone stops delivering audio, check the saved recording before retrying
recognition. A silent recording needs a working microphone; retrying a recognizer
cannot recover speech that was never captured. A stalled connection is reset in a separate audio helper, without restarting Maramax.
Received audio stays in the main app and is written to disk as it arrives.

## Local data

Settings, transcripts, audio, and diagnostic logs live under
`~/Library/Application Support/Maramax/`. Recordings are ordinary WAV files with
JSON metadata, retained up to 20 captures / 512 MB, always keeping the newest.
Export anything you want to keep permanently. **Clear History & Recordings…**
deletes retained transcripts and audio after confirmation.

Diagnostic logs rotate at 2 MB with two backups. They record operational errors
and measurements; normal transcript text is not deliberately logged. Errors may
include media filenames or microphone names. Models remain in the Hugging Face
cache after history is cleared. Media-file import uses the system FFmpeg tool,
which is already available on this Mac; dictation itself does not need it.

## Your first live check

Try a short sentence in a text field with the built-in microphone. Confirm that
the bar shows input and the text arrives. Then, when audio changes are convenient,
try AirPods: a dictation, a second one with the microphone kept connected, and
taking the AirPods out mid-sentence. These paths were verified with a simulated
audio device, not with real AirPods; the saved audio and measurements make any
remaining failure diagnosable.

This is a local, ad-hoc-signed build for this Mac. Public distribution would need
a separate signing and notarization release process.
