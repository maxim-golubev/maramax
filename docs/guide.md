# Using Maramax

Everything the app does and where it keeps things. For a first launch, see [the launch guide](LAUNCH.md).

## Everyday dictation

- Press the dictation shortcut (**Option+Space** unless you chose another) to
  start, then press it again or **Cmd+R** to finish. The first launch opens a
  short welcome that lets you pick another shortcut (and whether to paste);
  change it any time under **Settings → General → Shortcut**, or reopen the
  welcome with **Settings → General → Welcome Guide…**. Shortcuts macOS has
  turned on for itself (Spotlight's Cmd+Space, screenshots, and the rest of **Keyboard
  Shortcuts** in System Settings), combinations that type a character (Option+E)
  or that apps and Terminal use (Cmd+D, Control+C) are refused with the reason.
  Maramax cannot tell when another app uses a shortcut: if pressing yours opens
  something else, choose another.
- A small bar shows the selected microphone, captured duration, and input level
  without taking focus from your current app. Until the microphone delivers
  sound it says **Don’t speak yet** in orange, with an orange wave for a meter:
  anything said then would not be recorded. Bluetooth headphones take two or
  three seconds; the built-in microphone a fraction of a second. Stopping in
  that moment is not an error: it says nothing was recorded yet. Cmd+R is
  registered globally only while recording from the bar; it is released
  afterward.
- **Settings → General → When you finish dictating** decides where the
  transcript goes: **Paste into the app you’re using** (it is copied too),
  **Copy to the clipboard** (the default), or **Keep in Maramax only**, which
  copies nothing; Settings warns about that choice, and each dictation says the
  transcript was not copied. Pasting goes into the app you were last working
  in. From the bar it is skipped if you switch to a different app while
  dictating; the text stays copied. If the clipboard changes before pasting, or
  you cancel while it is transcribing, nothing is pasted and the transcript
  remains in history. Expanding the bar during an operation keeps its original
  destination app.
- Pasting after a word or a sentence puts a space in first, so dictations in a
  row do not run together; Maramax reads the character before the cursor for
  this, in apps that make it available (most text fields; not terminals).
- Pasting needs Maramax switched on under **Privacy & Security →
  Accessibility**. Choosing paste, or **Allow…** beside it, has macOS ask; its
  button opens that list with Maramax in it. Maramax first clears an entry left
  by an earlier build, which System Settings can show switched on while macOS
  refuses this one. Until it is allowed, transcripts are copied and the bar says
  so; Maramax never opens System Settings by itself.
- Use the arrow in the bar or **Open Transcript** for the full transcript, history,
  and file queue. Live transcription previews remain available in that window,
  and while it is open the shortcut dictates in it.
- Drag the bar by anything but its buttons to put it wherever suits you; it
  opens there from then on, on whichever display you are using. Double-click
  the bar, drop it back near the bottom centre, or choose **Reset Bar Position**
  in the menu (it is there while the bar has been moved) to return it to its
  default place.
- Capture continues for a fifth of a second after you press stop, so a last
  syllable still travelling through the driver or a Bluetooth link is not cut off.

**Settings…** has four tabs: General, Microphone, Words, and Advanced.
**Words** lists every word replacement, alphabetically: type a phrase the recognizer gets wrong and its desired spelling, then **Add**
(or Return). Words that already have a replacement are refused, and that one is
pointed out, rather than either rule being changed; double-click a replacement
in the list to change it, and **Remove** (or Delete) removes the selected ones,
with **Undo** to bring them back. **Try it** shows a sentence of yours with the
replacements applied.
Replacements match whole words or phrases without case sensitivity and apply
once, preferring longer phrases. They apply to dictation and recording retries;
the original transcript is retained in history and recording details. Imported
media is transcribed without these replacements. No additional language model
rewrites the text; the standard model's transcript only loses "um" and "uh" and
has its clock times written with a colon ("8:45 p.m."). Standard editing shortcuts (Cmd+V, Cmd+C,
Cmd+A, Cmd+Z, Cmd+W) work in Settings and the other windows.

With **Use the high-accuracy model** (Settings → Advanced) and **Apply my word
replacements** both on, the names and terms you entered as replacements are
also given to that model as vocabulary before it listens
(snippets, addresses, and lists are left out). On an M3 Pro it is several times
slower than the standard model (for dictations over two minutes, a median of
19.5 s against 2.3 s), so it stays off by default.

If the speech model cannot load, restore your connection and press your
shortcut again: that retries the download. So do **Retry Speech Model**, which
appears at the top of the menu while it is needed, and **Retry** in Settings →
Advanced; both also retry the high-accuracy model if its download failed. A second copy of Maramax is blocked before it loads models or opens
audio; quit the old copy before opening a different version.

## Audio and video files

Drop a file on the Maramax icon in the menu bar, or choose **Transcribe
Files…**, to transcribe it; several files go to the Queue tab of the Maramax
window, where **Start** asks where their transcripts should go. Files can also
be dropped on that window. A recording can be dragged straight out of Voice
Memos and other apps that hand over a file only when it is asked for; Maramax
keeps its copy of such a file until it is next opened (or until **Clear History
& Recordings…**), and cannot save a transcript "next to the original" for it.
Nothing can be dropped while you are dictating.

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
launch (about 125 ms saved per dictation on an M3 Pro). Microphone startup and
teardown run off the main UI thread. The bar distinguishes waiting for input
(orange, "Don’t speak yet"), digital silence, and a stream that stopped
delivering audio. Ordinary pauses
after signal has arrived do not count as disconnections.

If the microphone disappears or stops delivering audio mid-dictation, Automatic
mode reopens whichever input macOS now offers and continues the same recording;
the result notes that the microphone changed. A microphone you selected
explicitly is never swapped: the recording ends at once and what was captured is
kept. If the audio driver itself gets stuck, the helper process is replaced, so
the next dictation starts from a clean state.

Bluetooth microphones deliver one and a half to two and a half seconds of
silence each time they connect; that is the headset switching into call mode and
macOS gives apps no way to shorten it. **Keep the microphone connected** (Off,
For 30 seconds, For 2 minutes, For 5 minutes) leaves the stream open after a
dictation so the next one starts instantly. While it is open macOS shows the microphone indicator and
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
is applied by Maramax. The archive retains up to **20 recordings** (or the number chosen in Settings → Advanced) **and 512 MB**, keeping
the newest even if it alone exceeds that budget; older recordings are removed
as new ones are saved. Export recordings you want to keep permanently.

The live PCM recovery spill is written during capture for crash recovery.
At the next launch every leftover spill (one per capture that could not be
archived) is moved into Recordings as an ordinary entry, dated when it was
recorded. **Recover Last Recording** in the menu transcribes audio that never reached the recognizer first, then the
newest capture without a transcript, then an unsaved recording that could not be
moved into Recordings, and otherwise the newest recording. A recovery that
returns no text marks that recording as tried, so the next press moves on to
audio that has not been tried yet.
History keeps the last 100 transcripts and Recordings the last 20; **Keep the
last** in Settings → Advanced chooses 50 to 1,000 transcripts and 10 to 100
recordings. Recordings also stay under 512 MB in all. A lower number of
transcripts shows at once; with either, the oldest are deleted when the next
one is saved, so changing it back before then loses nothing.
**Settings → Advanced → Clear History & Recordings…** deletes both transcript
history and retained audio after confirmation, and is unavailable during an
active operation.

The original transcript for a replaced phrase is stored separately in
`history-originals.json`, keeping `history.json` readable by 0.3.0. Both are
cleared by **Clear History & Recordings…** in Settings → Advanced. Operational logs rotate at 2 MB with
two backups under `logs/`; no transcript text is deliberately logged.

Settings, history, and recording details written by a newer version keep their
extra fields when an older version saves them, so rolling back does not erase
anything. A settings or history file that cannot be read is renamed to
`*.corrupt` instead of being overwritten.

## Updates

Maramax checks GitHub for a newer release once a day (turn it off in **Settings
→ Advanced**); **Check for Updates…** in the menu or **Check Now** in Settings →
Advanced asks right away, and the top of Settings → General shows your
version. A newer version is offered in a **Software Update** window with its release notes; **Remind Me
Later** offers it again at the next check, and **Skip This Version** keeps the
daily check quiet about it. Choosing **Install Update** opens a small window
with the download's progress; Maramax then
restarts by itself as soon as it is idle (not dictating, transcribing, or
saving audio). An update downloads only the files that changed since your
version when it can, and is accepted only if it carries Maramax's own release
signature. The replaced version is kept for rollback under
`~/Library/Application Support/Maramax/updates/previous`. The check sends
nothing but the request and the installed version number.
