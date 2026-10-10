"""Tray icon image — redrawn per connection state; the color is the only
thing that changes.
"""
from __future__ import annotations

from PIL import Image, ImageColor, ImageDraw

DEFAULT_COLOR = "#00b894"  # same green PushToLag uses for "connected" before any state is known


def build_icon(size: int = 64, color: str = DEFAULT_COLOR) -> "Image.Image":
    image = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    try:
        accent = (*ImageColor.getrgb(color), 255)
    except ValueError:
        accent = (*ImageColor.getrgb(DEFAULT_COLOR), 255)
    pad = max(2, size // 16)
    draw.rounded_rectangle(
        (pad, pad, size - pad - 1, size - pad - 1),
        radius=max(6, size // 4), fill=accent,
    )
    white = (255, 255, 255, 255)
    # White lightning bolt, run diagonally corner-to-corner to use as much of
    # the box as possible — reads cleanly even at the 16px size Windows
    # commonly uses, and fits "instant cut" better than a signal glyph.
    draw.polygon(
        [(size * 0.66, size * 0.04), (size * 0.18, size * 0.60), (size * 0.42, size * 0.60),
         (size * 0.34, size * 0.96), (size * 0.82, size * 0.40), (size * 0.58, size * 0.40)],
        fill=white,
    )
    return image
