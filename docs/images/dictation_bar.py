"""Frames of the dictation bar through one dictation, drawn off-screen by the app's own views.

The dictation is the one the welcome plays (bar_demo.dictation), one frame per update of the bar.

    .venv/bin/python docs/images/dictation_bar.py <frames-dir> NSAppearanceNameAqua   (or NSAppearanceNameDarkAqua)
    ffmpeg -framerate 20/3 -i <frames-dir>/f%03d.png -filter_complex \
      "color=0xffffff:s=920x168:r=20/3[bg];[bg][0]overlay=20:20:shortest=1,split[a][b];[a]palettegen=max_colors=128[p];[b][p]paletteuse=dither=none" \
      -loop 0 docs/images/dictation-light.gif                                   (0x0d1117 for dark)
"""

import sys
from pathlib import Path

sys.path.insert(0, "src")

from AppKit import (  # noqa: E402
    NSAppearance, NSApplication, NSApplicationActivationPolicyProhibited, NSBitmapImageFileTypePNG,
)

from parakeet_dictation.bar_demo import dictation  # noqa: E402
from parakeet_dictation.hotkeys import DEFAULT_DICTATE  # noqa: E402
from parakeet_dictation.indicator import DictationIndicator  # noqa: E402


def main() -> None:
    out, appearance = Path(sys.argv[1]), sys.argv[2]
    NSApplication.sharedApplication().setActivationPolicy_(NSApplicationActivationPolicyProhibited)
    # The bar is drawn from its view; its panel is never put on screen here.
    bar = DictationIndicator.alloc().initWithDelegate_(None)
    view = bar.panel.contentView()
    view.setAppearance_(NSAppearance.appearanceNamed_(appearance))
    view.refresh_background()
    frames = dictation(DEFAULT_DICTATE.label)
    for index, frame in enumerate(frames):
        frame(bar)
        view.layoutSubtreeIfNeeded()
        rep = view.bitmapImageRepForCachingDisplayInRect_(view.bounds())
        view.cacheDisplayInRect_toBitmapImageRep_(view.bounds(), rep)
        png = rep.representationUsingType_properties_(NSBitmapImageFileTypePNG, None)
        png.writeToFile_atomically_(str(out / f"f{index:03d}.png"), True)
    print(len(frames), "frames")


if __name__ == "__main__":
    main()
