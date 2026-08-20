"""Image helpers for inbound file attachments."""

from __future__ import annotations

import logging

log = logging.getLogger(__name__)

# Anthropic's API caps images at 2000px per side once a conversation
# carries more than ~20 of them; one oversized image then fails every
# request in the session. Modern iPhone screenshots (e.g. 1179x2556)
# are the common offender, so they get downscaled at download time.
MAX_DIMENSION = 2000

# Modern iPhone screens are ~19.5:9 through ~2.4:1. Screenshots are
# PNGs; camera photos (JPEG/HEIC) and other images are left untouched.
_MIN_ASPECT = 1.9
_MAX_ASPECT = 2.4


def downscale_iphone_screenshot(path: str, max_dim: int = MAX_DIMENSION) -> bool:
    """Resize an iPhone screenshot in place so its long side fits max_dim.

    Only files that look like phone screenshots are touched: PNG format,
    screenshot-shaped aspect ratio, and a long side over the limit.
    Returns True when the file was resized. Any failure (Pillow missing,
    corrupt file) leaves the file as downloaded and returns False —
    downloads must never break over this.
    """
    try:
        from PIL import Image
    except ImportError:
        log.debug("Pillow not installed; skipping screenshot downscale")
        return False

    try:
        with Image.open(path) as img:
            if img.format != "PNG":
                return False
            width, height = img.size
            long_side, short_side = max(width, height), min(width, height)
            if long_side <= max_dim or short_side == 0:
                return False
            if not _MIN_ASPECT <= long_side / short_side <= _MAX_ASPECT:
                return False
            scale = max_dim / long_side
            new_size = (round(width * scale), round(height * scale))
            resized = img.resize(new_size, Image.Resampling.LANCZOS)
        resized.save(path, format="PNG")
        log.info(
            "Downscaled screenshot %s from %dx%d to %dx%d",
            path,
            width,
            height,
            *new_size,
        )
        return True
    except Exception:
        log.exception("Failed to downscale %s", path)
        return False
