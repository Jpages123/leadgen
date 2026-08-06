"""Screenshot helper — full-page PNG capture for LLM vision review.

Used by mockup_screenshot (mockup-builder.ts) so the LLM can visually inspect
the deployed mockup (or the prospect's original site) before approving it.
Unlike app/utils/mockup_screenshot.py (email embedding, resized JPEG upload),
this returns a full-resolution PNG as base64 for direct inclusion in the
LLM's context — no upload, no resize, no compression artifacts to hide
cropping/layout bugs.
"""
from __future__ import annotations

import base64

from playwright.sync_api import sync_playwright

VIEWPORTS = {
    "desktop": {"width": 1440, "height": 900},
    "mobile": {"width": 390, "height": 844},
}


def screenshot(url: str, viewport: str = "desktop", timeout_ms: int = 20_000) -> dict:
    """Capture a full-page PNG screenshot of ``url``.

    Returns a JSON-serializable dict: {"png_base64": str, "width": int, "height": int}.
    Raises on navigation failure or timeout — the caller (the LLM, via the tool
    wrapper) sees this as a tool error and can retry or report it, rather than
    silently getting an empty/placeholder image.
    """
    vp = VIEWPORTS.get(viewport, VIEWPORTS["desktop"])
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True, args=["--no-sandbox"])
        try:
            page = browser.new_page(viewport=vp)
            page.goto(url, wait_until="networkidle", timeout=timeout_ms)
            png_bytes = page.screenshot(full_page=True)
        finally:
            browser.close()
    return {
        "png_base64": base64.b64encode(png_bytes).decode("ascii"),
        "width": vp["width"],
        "height": vp["height"],
    }
