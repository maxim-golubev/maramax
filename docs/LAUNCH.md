# Maramax 0.4.1 — ready for your first launch

## Open the new version

1. Quit the currently running Maramax using its menu bar icon.
2. Open `Maramax.app` beside this guide. The app appears in the menu bar.
3. Wait for the speech model to be ready, then press **Option+Space** to start
   dictating. Press **Option+Space** again, or **Cmd+R**, to finish.
4. Paste the copied transcript with **Cmd+V**. For automatic insertion, enable
   **Paste Into Active App** in **Settings…** and grant the macOS Accessibility
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

## Everyday use

- **Compact bar:** stays out of the way and leaves your current app focused.
  Its meter reflects incoming audio, and its timer shows captured duration.
- **Open Transcript:** opens the larger transcript, history,
  and file queue. The arrow in the compact bar opens the same controls.
- **Settings…:** controls automatic copy/paste, preview, input preference,
  recognition model, and word replacements. Add a heard phrase and its desired
  replacement; edit or remove it at any time. Original text stays available in
  history and saved recordings.
- **Recordings…:** play, export, or retry saved captures, including
  recordings that returned no transcript. A retry copies its result without
  inserting it into another app.

Automatic input prefers the Mac microphone. Wearing AirPods does not automatically
select their microphone. To use it, choose AirPods in Settings → Microphone.
Using a Bluetooth microphone can change headphone playback quality; do that test
when it is convenient to interrupt your movie or music.

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
which is already available on this Mac.

## Your first live check

Try a short sentence in a text field with the built-in microphone. Confirm that
the bar shows input and the text arrives. Then, when audio changes are convenient,
try AirPods and a reconnect. Automated and file-based tests cannot establish that
the intermittent AirPods problem is fixed; the saved audio and measurements make
any remaining failure diagnosable.

This is a local, ad-hoc-signed build for this Mac. Public distribution would need
a separate signing and notarization release process.
