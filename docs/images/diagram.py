"""Draw the README's diagram as plain SVG (GitHub adds pan and zoom buttons over Mermaid).

    python docs/images/diagram.py docs/images
"""
import sys
from pathlib import Path

THEMES = {
    "light": dict(box="#f6f8fa", edge="#d0d7de", text="#1f2328", note="#59636e", arrow="#59636e",
                  accent="#ddf4ff", accent_edge="#54aeff"),
    "dark": dict(box="#151b23", edge="#3d444d", text="#f0f6fc", note="#9198a1", arrow="#9198a1",
                 accent="#0d2a4d", accent_edge="#1f6feb"),
}
W, H, BOX_H = 980, 250, 54
FONT = "-apple-system, BlinkMacSystemFont, 'Segoe UI', Helvetica, Arial, sans-serif"

def box(c, x, y, w, lines, accent=False):
    fill, edge = (c["accent"], c["accent_edge"]) if accent else (c["box"], c["edge"])
    out = [f'<rect x="{x}" y="{y}" width="{w}" height="{BOX_H}" rx="10" fill="{fill}" stroke="{edge}" stroke-width="1.5"/>']
    first = y + BOX_H / 2 + (5 if len(lines) == 1 else -4)
    for i, (text, small) in enumerate(lines):
        size, color = (12, c["note"]) if small else (14, c["text"])
        weight = "600" if accent and not small else "400"
        out.append(f'<text x="{x + w / 2}" y="{first + i * 18}" text-anchor="middle" font-size="{size}" '
                   f'font-weight="{weight}" fill="{color}">{text}</text>')
    return "\n".join(out)

def arrow(c, x1, y1, x2, y2, label=None, lx=None, ly=None, anchor="middle"):
    out = [f'<line x1="{x1}" y1="{y1}" x2="{x2}" y2="{y2}" stroke="{c["arrow"]}" stroke-width="1.5" marker-end="url(#head)"/>']
    if label:
        out.append(f'<text x="{lx}" y="{ly}" text-anchor="{anchor}" font-size="12" fill="{c["note"]}">{label}</text>')
    return "\n".join(out)

def diagram(c):
    top, bottom = 30, 170
    parts = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{W}" height="{H}" viewBox="0 0 {W} {H}" font-family="{FONT}">',
        f'<defs><marker id="head" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" orient="auto-start-reverse">'
        f'<path d="M 0 0 L 10 5 L 0 10 z" fill="{c["arrow"]}"/></marker></defs>',
        box(c, 10, top, 130, [("Option+Space", False)]),
        box(c, 190, top, 130, [("Maramax", False)], accent=True),
        box(c, 430, top, 210, [("Parakeet TDT 0.6B v2", False), ("MLX, on the GPU", True)]),
        box(c, 680, top, 150, [("Word replacements", False)]),
        box(c, 860, top, 110, [("Clipboard", False), ("or paste", True)]),
        box(c, 120, bottom, 270, [("Audio helper process", False), ("owns the microphone", True)]),
        box(c, 500, bottom, 200, [("Saved WAV", False), ("before recognition", True)]),
        arrow(c, 140, top + 27, 186, top + 27),
        arrow(c, 320, top + 27, 426, top + 27, "from memory", 373, top + 19),
        arrow(c, 640, top + 27, 676, top + 27),
        arrow(c, 830, top + 27, 856, top + 27),
        arrow(c, 236, top + BOX_H, 236, bottom - 4, "record / stop", 228, 133, "end"),
        arrow(c, 276, bottom, 276, top + BOX_H + 4, "audio, over a pipe", 284, 133, "start"),
        arrow(c, 320, top + BOX_H - 8, 550, bottom - 4, "archived first", 470, 128, "start"),
        "</svg>",
    ]
    return "\n".join(parts) + "\n"

for name, colors in THEMES.items():
    Path(sys.argv[1], f"how-it-works-{name}.svg").write_text(diagram(colors))
