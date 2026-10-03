"""Renders the app icon document (packaging/Maramax.icon) into the committed image files listed below.

    .venv/bin/python packaging/app_icon.py

Needs Xcode 26 (actool, and Icon Composer's ictool). Run it after editing the icon; the build only copies its
outputs, so a machine without Xcode can still build the app.

- packaging/Assets.car: the compiled icon. macOS 26 and later draw it with Liquid Glass in every appearance
  (default, dark, clear, tinted).
- packaging/Maramax.icns: the default appearance at every size up to 1024 px, for macOS 15 and earlier.
- docs/images/icon-light.png, icon-dark.png: the README's icon.
"""

import subprocess
import tempfile
from pathlib import Path

from AppKit import (
    NSBitmapImageRep, NSColor, NSCompositingOperationSourceOver, NSDeviceRGBColorSpace, NSGraphicsContext,
    NSImage, NSMakeRect, NSMakeSize, NSPNGFileType, NSShadow,
)

ROOT = Path(__file__).resolve().parents[1]
DOCUMENT = ROOT / "packaging" / "Maramax.icon"
ICTOOL = Path("/Applications/Xcode.app/Contents/Applications/Icon Composer.app/Contents/Executables/ictool")
# Apple's macOS icon grid: on a 1024 px canvas the tile is 824 px, raised slightly above its shadow.
CANVAS, TILE, LIFT = 1024, 824, 10
ICNS_POINTS = (16, 32, 128, 256, 512)
README_PIXELS = 256
# On a web page the grid's margin is only empty space: the README's icon keeps just room for the shadow.
README_TILE = 944


def render_tile(rendition: str, pixels: int, path: Path) -> None:
    """The tile alone, masked to its rounded shape, as ictool draws it."""
    subprocess.run([str(ICTOOL), str(DOCUMENT), "--export-image", "--output-file", str(path), "--platform", "macOS",
                    "--rendition", rendition, "--width", str(pixels), "--height", str(pixels), "--scale", "1"],
                   check=True, capture_output=True)


def icon_png(tile_png: Path, pixels: int, tile_size: int = TILE) -> bytes:
    """The tile placed on the icon grid (`tile_size` of the 1024 canvas) with the system's drop shadow."""
    rep = NSBitmapImageRep.alloc().initWithBitmapDataPlanes_pixelsWide_pixelsHigh_bitsPerSample_samplesPerPixel_hasAlpha_isPlanar_colorSpaceName_bytesPerRow_bitsPerPixel_(  # noqa: E501
        None, pixels, pixels, 8, 4, True, False, NSDeviceRGBColorSpace, 0, 0)
    rep.setSize_(NSMakeSize(pixels, pixels))
    scale = pixels / CANVAS
    margin = (CANVAS - tile_size) / 2 * scale
    NSGraphicsContext.saveGraphicsState()
    NSGraphicsContext.setCurrentContext_(NSGraphicsContext.graphicsContextWithBitmapImageRep_(rep))
    shadow = NSShadow.alloc().init()
    shadow.setShadowColor_(NSColor.colorWithWhite_alpha_(0, 0.3))
    shadow.setShadowOffset_(NSMakeSize(0, -LIFT * scale))
    shadow.setShadowBlurRadius_(24 * scale)
    shadow.set()
    tile = NSImage.alloc().initWithContentsOfFile_(str(tile_png))
    tile.drawInRect_fromRect_operation_fraction_(
        NSMakeRect(margin, margin + LIFT * scale, tile_size * scale, tile_size * scale), NSMakeRect(0, 0, 0, 0),
        NSCompositingOperationSourceOver, 1.0)
    NSGraphicsContext.restoreGraphicsState()
    return bytes(rep.representationUsingType_properties_(NSPNGFileType, None))


def write_icns(scratch: Path) -> None:
    iconset = scratch / "Maramax.iconset"
    iconset.mkdir()
    for points in ICNS_POINTS:
        for factor in (1, 2):
            pixels = points * factor
            tile = scratch / f"tile-{pixels}.png"
            if not tile.exists():
                render_tile("Default", round(pixels * TILE / CANVAS), tile)
            name = f"icon_{points}x{points}{'@2x' if factor == 2 else ''}.png"
            (iconset / name).write_bytes(icon_png(tile, pixels))
    subprocess.run(["iconutil", "--convert", "icns", "--output", str(ROOT / "packaging" / "Maramax.icns"),
                    str(iconset)], check=True)


def write_assets_car(scratch: Path) -> None:
    compiled = scratch / "compiled"
    compiled.mkdir()
    subprocess.run(["xcrun", "actool", str(DOCUMENT), "--compile", str(compiled), "--platform", "macosx",
                    "--minimum-deployment-target", "12.0", "--app-icon", "Maramax",
                    "--output-partial-info-plist", str(scratch / "partial.plist")],
                   check=True, capture_output=True)
    (ROOT / "packaging" / "Assets.car").write_bytes((compiled / "Assets.car").read_bytes())


def write_readme_icons(scratch: Path) -> None:
    for rendition, name in (("Default", "light"), ("Dark", "dark")):
        tile = scratch / f"readme-{name}.png"
        render_tile(rendition, round(README_PIXELS * README_TILE / CANVAS), tile)
        (ROOT / "docs" / "images" / f"icon-{name}.png").write_bytes(icon_png(tile, README_PIXELS, README_TILE))


if __name__ == "__main__":
    with tempfile.TemporaryDirectory() as directory:
        scratch = Path(directory)
        write_assets_car(scratch)
        write_icns(scratch)
        write_readme_icons(scratch)
