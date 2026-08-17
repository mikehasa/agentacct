#!/usr/bin/env python3
"""Wrap a flat pane PNG in a macOS window chrome — traffic lights, a title bar,
rounded corners, and a soft drop shadow — on a transparent background.

    python scripts/frame_screenshots.py                 # frames docs/assets/app-*.png in place
    python scripts/frame_screenshots.py a.png b.png      # frames the given files in place

Input is a 2x retina pane render (e.g. from gen_app_screenshots.py); the output
overwrites it with the framed version. Transparent PNG so the shot sits cleanly
on a light or dark README. Deterministic (Pillow only — no browser).
"""
from __future__ import annotations

import sys
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter, ImageFont

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULTS = ["app-dashboard.png", "app-work-receipt.png", "app-usage.png"]

# Geometry (px, at the 2x scale of the input renders).
BAR_H = 56          # title bar height
RADIUS = 24         # window corner radius
PAD = 90            # transparent margin around the window (room for the shadow)
DOT_R = 12          # traffic-light radius
TITLE = "agentacct"


def _system_font(size: int) -> ImageFont.FreeTypeFont | None:
    for path in ("/System/Library/Fonts/SFNS.ttf",
                 "/System/Library/Fonts/Helvetica.ttc",
                 "/System/Library/Fonts/Supplemental/Arial.ttf"):
        try:
            return ImageFont.truetype(path, size)
        except Exception:
            continue
    return None


def _titlebar(width: int) -> Image.Image:
    """A light vertical-gradient title bar with the three traffic lights and a
    centered window title."""
    bar = Image.new("RGBA", (width, BAR_H), (0, 0, 0, 0))
    top, bot = (247, 247, 248), (235, 235, 238)
    px = bar.load()
    for y in range(BAR_H):
        t = y / (BAR_H - 1)
        c = tuple(round(top[i] + (bot[i] - top[i]) * t) for i in range(3))
        for x in range(width):
            px[x, y] = (*c, 255)
    d = ImageDraw.Draw(bar)
    d.line([(0, BAR_H - 1), (width, BAR_H - 1)], fill=(0, 0, 0, 26))
    cy = BAR_H // 2
    x = 24 + DOT_R
    for col in ((255, 95, 87), (254, 188, 46), (40, 200, 64)):
        d.ellipse([x - DOT_R, cy - DOT_R, x + DOT_R, cy + DOT_R], fill=(*col, 255))
        d.ellipse([x - DOT_R, cy - DOT_R, x + DOT_R, cy + DOT_R], outline=(0, 0, 0, 18), width=1)
        x += 2 * DOT_R + 16
    font = _system_font(27)
    if font is not None:
        tb = d.textbbox((0, 0), TITLE, font=font)
        d.text(((width - (tb[2] - tb[0])) / 2, (BAR_H - (tb[3] - tb[1])) / 2 - tb[1]),
               TITLE, font=font, fill=(95, 95, 104, 255))
    return bar


def frame(path: Path) -> None:
    content = Image.open(path).convert("RGBA")
    w, h = content.size
    win_h = BAR_H + h

    # The window: title bar on top, the pane below, rounded corners.
    win = Image.new("RGBA", (w, win_h), (255, 255, 255, 255))
    win.paste(_titlebar(w), (0, 0))
    win.paste(content, (0, BAR_H))
    corner = Image.new("L", (w, win_h), 0)
    ImageDraw.Draw(corner).rounded_rectangle([0, 0, w - 1, win_h - 1], radius=RADIUS, fill=255)
    win.putalpha(corner)

    # Compose onto a transparent canvas with a soft drop shadow.
    cw, ch = w + 2 * PAD, win_h + 2 * PAD
    canvas = Image.new("RGBA", (cw, ch), (0, 0, 0, 0))
    shadow_mask = Image.new("L", (cw, ch), 0)
    dy = 26  # push the shadow down for a lit-from-above look
    ImageDraw.Draw(shadow_mask).rounded_rectangle(
        [PAD, PAD + dy, PAD + w - 1, PAD + win_h - 1 + dy], radius=RADIUS, fill=96)
    shadow_mask = shadow_mask.filter(ImageFilter.GaussianBlur(38))
    shadow = Image.new("RGBA", (cw, ch), (0, 0, 0, 0))
    shadow.putalpha(shadow_mask)
    canvas = Image.alpha_composite(canvas, shadow)
    canvas.alpha_composite(win, (PAD, PAD))
    canvas.save(path)
    print(f"  framed {path.name} ({w}x{h} -> {cw}x{ch})")


def main() -> None:
    args = sys.argv[1:]
    targets = [Path(a) for a in args] if args else [REPO_ROOT / "docs" / "assets" / n for n in DEFAULTS]
    for t in targets:
        if not t.exists():
            print(f"  WARNING: missing {t}")
            continue
        frame(t)


if __name__ == "__main__":
    main()
