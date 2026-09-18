"""Pillow-based placeholder image generators.

Used as the LAST step in the mockup image fallback chain — when scraped
assets are missing AND Pexels fails, we generate a deterministic,
always-available placeholder locally. No network calls, no external
dependencies beyond Pillow (already in the venv).

The placeholders are intentionally branded:
- ``text_logo`` renders the business name in white on the brand accent
  colour, in a modern bold sans-serif. This is what the owner sees in
  the site header; it's a real, defensible look.
- ``solid_image`` and ``gradient_image`` produce full-bleed backdrops
  suitable for hero / about / gallery slots where we'd rather show a
  clean colour wash than a broken-image icon.

Every function returns the size of the file written (for the caller's
post-condition checks) and raises ``RuntimeError`` on Pillow failure
so the build fails fast instead of deploying an empty file.
"""
from __future__ import annotations

import math
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

# ─── Internal helpers ────────────────────────────────────────────────────────

def _hex_to_rgb(hex_str: str) -> tuple[int, int, int]:
    """Parse ``#1a4d5c`` / ``1a4d5c`` → ``(26, 77, 92)``. Falls back to slate."""
    s = (hex_str or "").lstrip("#")
    if len(s) != 6:
        return (26, 77, 92)  # slate fallback
    try:
        return (int(s[0:2], 16), int(s[2:4], 16), int(s[4:6], 16))
    except ValueError:
        return (26, 77, 92)


def _contrast_text_color(rgb: tuple[int, int, int]) -> tuple[int, int, int]:
    """Return black or white depending on luminance for AA-contrast text."""
    luminance = (0.299 * rgb[0] + 0.587 * rgb[1] + 0.114 * rgb[2]) / 255
    return (255, 255, 255) if luminance < 0.6 else (20, 20, 20)


def _load_font(size: int) -> ImageFont.ImageFont:
    """Find a usable bold sans-serif. Falls back to Pillow's default bitmap."""
    candidates = [
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
        "/usr/share/fonts/TTF/DejaVuSans-Bold.ttf",
        "/Library/Fonts/Arial Bold.ttf",
    ]
    for path in candidates:
        if Path(path).exists():
            try:
                return ImageFont.truetype(path, size)
            except Exception:
                continue
    return ImageFont.load_default()


# ─── Public API ──────────────────────────────────────────────────────────────

def text_logo(
    business_name: str,
    accent_hex: str,
    dest: str | Path,
    width: int = 480,
    height: int = 120,
) -> int:
    """Render a branded text logo (business name on accent-colour background).

    Returns bytes written. Raises ``RuntimeError`` on failure.
    """
    bg = _hex_to_rgb(accent_hex)
    fg = _contrast_text_color(bg)

    img = Image.new("RGB", (width, height), bg)
    draw = ImageDraw.Draw(img)

    # Scale font to fit. Roughly 60% of height is the cap height, and the
    # business name should fit in 80% of width.
    name = (business_name or "").strip() or "Business"
    max_font_size = int(height * 0.55)
    min_font_size = 14
    chosen_font = _load_font(max_font_size)
    # If the text overflows, shrink the font.
    bbox = draw.textbbox((0, 0), name, font=chosen_font)
    text_width = bbox[2] - bbox[0]
    if text_width > width * 0.85:
        scale = (width * 0.85) / text_width
        new_size = max(min_font_size, int(max_font_size * scale))
        chosen_font = _load_font(new_size)
        bbox = draw.textbbox((0, 0), name, font=chosen_font)
        text_width = bbox[2] - bbox[0]
        text_height = bbox[3] - bbox[1]

    x = (width - text_width) // 2 - bbox[0]
    y = (height - (bbox[3] - bbox[1])) // 2 - bbox[1]
    draw.text((x, y), name, fill=fg, font=chosen_font)

    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    img.save(dest, "JPEG", quality=88)
    return dest.stat().st_size


def solid_image(
    hex_color: str,
    dest: str | Path,
    width: int = 1920,
    height: int = 1080,
    label: str | None = None,
) -> int:
    """Solid-colour full-bleed image. Optional centred label for hero/about slots."""
    bg = _hex_to_rgb(hex_color)
    img = Image.new("RGB", (width, height), bg)
    if label:
        draw = ImageDraw.Draw(img)
        font = _load_font(int(height * 0.08))
        bbox = draw.textbbox((0, 0), label, font=font)
        tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
        fg = _contrast_text_color(bg)
        draw.text(
            ((width - tw) // 2 - bbox[0], (height - th) // 2 - bbox[1]),
            label,
            fill=fg,
            font=font,
        )
    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    img.save(dest, "JPEG", quality=85)
    return dest.stat().st_size


def gradient_image(
    hex_color: str,
    dest: str | Path,
    width: int = 1920,
    height: int = 1080,
    label: str | None = None,
) -> int:
    """Vertical gradient from accent colour to a darker shade, plus optional label."""
    r, g, b = _hex_to_rgb(hex_color)
    img = Image.new("RGB", (width, height))
    px = img.load()
    for y in range(height):
        t = y / max(1, height - 1)
        # Darken to 35% of original at the bottom.
        nr = max(0, int(r * (1 - 0.65 * t)))
        ng = max(0, int(g * (1 - 0.65 * t)))
        nb = max(0, int(b * (1 - 0.65 * t)))
        for x in range(width):
            px[x, y] = (nr, ng, nb)
    if label:
        draw = ImageDraw.Draw(img)
        font = _load_font(int(height * 0.08))
        bbox = draw.textbbox((0, 0), label, font=font)
        tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
        # Label gets a slight darken band behind it for legibility
        bg_y0 = (height - th) // 2 - bbox[1] - 20
        bg_y1 = bg_y0 + th + 40
        overlay = Image.new("RGBA", (width, height), (0, 0, 0, 0))
        odraw = ImageDraw.Draw(overlay)
        odraw.rectangle([(0, bg_y0), (width, bg_y1)], fill=(0, 0, 0, 80))
        img = Image.alpha_composite(img.convert("RGBA"), overlay).convert("RGB")
        draw = ImageDraw.Draw(img)
        draw.text(
            ((width - tw) // 2 - bbox[0], (height - th) // 2 - bbox[1]),
            label,
            fill=(255, 255, 255),
            font=font,
        )
    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    img.save(dest, "JPEG", quality=85)
    return dest.stat().st_size