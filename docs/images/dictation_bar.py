"""Frames of the dictation bar through one dictation, drawn off-screen by the app's own views.

One frame per update of the bar, as the app makes them: every 150 ms while recording.

    .venv/bin/python docs/images/dictation_bar.py <frames-dir> NSAppearanceNameAqua   (or NSAppearanceNameDarkAqua)
    ffmpeg -framerate 20/3 -i <frames-dir>/f%03d.png -filter_complex \
      "color=0xffffff:s=920x168:r=20/3[bg];[bg][0]overlay=20:20:shortest=1,split[a][b];[a]palettegen=max_colors=128[p];[b][p]paletteuse=dither=none" \
      -loop 0 docs/images/dictation-light.gif                                   (0x0d1117 for dark)
"""

import math
import sys
from pathlib import Path

sys.path.insert(0, "src")

from AppKit import (  # noqa: E402
    NSAppearance, NSApplication, NSApplicationActivationPolicyProhibited, NSBitmapImageFileTypePNG,
)

from parakeet_dictation.app import BAR_SECONDS_AFTER_SUCCESS, WAIT_TO_SPEAK_STATUS  # noqa: E402
from parakeet_dictation.capture import CaptureSnapshot  # noqa: E402
from parakeet_dictation.indicator import DictationIndicator  # noqa: E402

UPDATE_SECONDS = 0.15  # How often the app updates the bar while recording (DictationApp._monitor_capture).


def capture(seconds: float, level: float, sound: bool = True) -> CaptureSnapshot:
    """What the meter reports; without `sound`, the silence a microphone sends while it connects."""
    return CaptureSnapshot(elapsed=seconds, audio_seconds=seconds, first_frame_delay=0.26, last_frame_age=0.02,
                           last_signal_age=0.02 if sound else None, peak=0.4 if sound else 0.0, level=level,
                           nonzero_samples=1 if sound else 0, callbacks=1, overflow_count=0,
                           device_name="MacBook Pro Microphone")


def speech_level(seconds: float) -> float:
    """The meter's level for someone speaking steadily, shaped like the app's
    meter over real dictations: mostly between 0.5 and 0.75, moving about a
    tenth from one update to the next."""
    return 0.62 + 0.08 * math.sin(2 * math.pi * 0.5 * seconds) + 0.06 * math.sin(2 * math.pi * 1.9 * seconds + 0.7)


def updates(seconds: float) -> int:
    return round(seconds / UPDATE_SECONDS)


def main() -> None:
    out, appearance = Path(sys.argv[1]), sys.argv[2]
    NSApplication.sharedApplication().setActivationPolicy_(NSApplicationActivationPolicyProhibited)
    # A new bar is laid out for recording; it is never put on screen here.
    bar = DictationIndicator.alloc().initWithDelegate_(None)
    view = bar.panel.contentView()
    view.setAppearance_(NSAppearance.appearanceNamed_(appearance))
    view.refresh_background()
    written = 0

    def frames(count: int = 1) -> None:
        nonlocal written
        view.layoutSubtreeIfNeeded()
        rep = view.bitmapImageRepForCachingDisplayInRect_(view.bounds())
        view.cacheDisplayInRect_toBitmapImageRep_(view.bounds(), rep)
        png = rep.representationUsingType_properties_(NSBitmapImageFileTypePNG, None)
        for _ in range(count):
            png.writeToFile_atomically_(str(out / f"f{written:03d}.png"), True)
            written += 1

    bar.set_status(WAIT_TO_SPEAK_STATUS)
    for _ in range(updates(2.0)):                # AirPods connecting: about two seconds of silence.
        bar.set_capture(capture(0.0, 0.0, sound=False))
        frames()
    bar.set_status("Recording…")
    for step in range(updates(4.2)):
        t = step * UPDATE_SECONDS
        bar.set_capture(capture(t, speech_level(t)))
        frames()
    bar.set_transcribing()
    frames(updates(0.6))
    bar.finish("Copied transcript to clipboard", BAR_SECONDS_AFTER_SUCCESS)
    frames(updates(BAR_SECONDS_AFTER_SUCCESS))  # Then the bar goes, and the loop starts the next dictation.
    print(written, "frames")


if __name__ == "__main__":
    main()
