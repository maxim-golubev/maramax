"""Frames of the dictation bar through one dictation, drawn off-screen by the app's own views.

    .venv/bin/python docs/images/dictation_bar.py <frames-dir> NSAppearanceNameAqua   (or NSAppearanceNameDarkAqua)
    ffmpeg -framerate 10 -i <frames-dir>/f%03d.png -filter_complex \
      "[0]pad=920:168:20:20:color=0xffffff,split[a][b];[a]palettegen=max_colors=128[p];[b][p]paletteuse=dither=none" \
      -loop 0 docs/images/dictation-light.gif                                   (0x0d1117 for dark)
"""

import math
import random
import sys
from pathlib import Path

sys.path.insert(0, "src")

from AppKit import (  # noqa: E402
    NSAppearance, NSApplication, NSApplicationActivationPolicyProhibited, NSBitmapImageFileTypePNG,
)

from parakeet_dictation.capture import CaptureSnapshot  # noqa: E402
from parakeet_dictation.indicator import DictationIndicator  # noqa: E402


def capture(seconds: float, level: float) -> CaptureSnapshot:
    return CaptureSnapshot(elapsed=seconds, audio_seconds=seconds, first_frame_delay=0.26, last_frame_age=0.02,
                           last_signal_age=0.02, peak=0.4, level=level, nonzero_samples=1, callbacks=1,
                           overflow_count=0, device_name="MacBook Pro Microphone")


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

    bar.set_status("Connecting microphone…")
    bar.detail.setStringValue_("Option+Space or Cmd+R to finish")
    frames(4)
    bar.set_status("Recording…")
    random.seed(7)
    for step in range(42):                       # 4.2 s of speech at 10 frames a second
        t = step / 10
        syllables = 0.55 + 0.35 * math.sin(t * 9.0) * math.sin(t * 2.3)
        level = 0.04 if 2.1 < t < 2.5 else max(0.05, min(1.0, syllables + random.uniform(-0.15, 0.15)))
        bar.set_capture(capture(t, level))
        frames()
    bar.set_transcribing()
    frames(5)
    bar.finish("Copied transcript to clipboard", 2.0)
    frames(18)
    print(written, "frames")


if __name__ == "__main__":
    main()
