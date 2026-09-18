"""Mockup screenshot capture + upload to VPS for email embedding.

Captures a screenshot of the live mockup URL via Playwright, then uploads
it to login.clientcompass.co.za via HTTPS POST. The bytes are saved
under /home/this0ne/installedApps/whatsapp_bot/site/_site/assets/mockups/
via a bind mount, and served at https://clientcompass.co.za/assets/mockups/<file>.

Gmail strips <img src="data:..."> tags (security measure), so we MUST host
the screenshot publicly for Gmail recipients to see it.

Returns a (reference, is_url) tuple:
    - (public_url, True)  → use <img src="https://...">
    - (data_uri, False)   → use <img src="data:image/...">  (Apple Mail only)
    - (None, False)       → no screenshot, use CTA fallback
"""
from __future__ import annotations

import asyncio
import base64
import io
import urllib.error
import urllib.request

from app.config import get_settings
from app.utils.logger import get_logger

log = get_logger(__name__)


def _is_safe_url(url: str) -> bool:
    if not url or not isinstance(url, str):
        return False
    url = url.strip()
    if not url.startswith(("http://", "https://")):
        return False
    bad_hosts = ("localhost", "127.0.0.1", "0.0.0.0", "::1", "10.", "192.168.", "172.16.")
    return not any(b in url for b in bad_hosts)


async def _capture_async(
    mockup_url: str,
    max_width: int = 500,
    jpeg_quality: int = 65,
    timeout_ms: int = 30000,
):
    try:
        from playwright.async_api import async_playwright
    except ImportError:
        log.warning("screenshot_playwright_not_available")
        return None
    try:
        async with async_playwright() as pw:
            browser = await pw.chromium.launch(headless=True, args=["--no-sandbox"])
            context = await browser.new_context(
                viewport={"width": max_width, "height": 800},
                device_scale_factor=1,
                user_agent="Mozilla/5.0 (compatible; ClientCompassBot/1.0; +https://clientcompass.co.za)",
            )
            page = await context.new_page()
            try:
                await page.goto(mockup_url, wait_until="networkidle", timeout=timeout_ms)
                await page.wait_for_timeout(500)
                png_bytes = await page.screenshot(full_page=False, type="png")
            finally:
                await context.close()
                await browser.close()
        try:
            from PIL import Image
            img = Image.open(io.BytesIO(png_bytes))
            if img.width > max_width:
                ratio = max_width / img.width
                new_size = (max_width, int(img.height * ratio))
                img = img.resize(new_size, Image.LANCZOS)
            if img.mode in ("RGBA", "LA", "P"):
                bg = Image.new("RGB", img.size, (255, 255, 255))
                if img.mode == "P":
                    img = img.convert("RGBA")
                bg.paste(img, mask=img.split()[-1] if img.mode in ("RGBA", "LA") else None)
                img = bg
            elif img.mode != "RGB":
                img = img.convert("RGB")
            out = io.BytesIO()
            img.save(out, format="JPEG", quality=jpeg_quality, optimize=True)
            jpeg_bytes = out.getvalue()
            log.info(
                "screenshot_captured",
                url=mockup_url,
                width=img.width,
                height=img.height,
                bytes=len(jpeg_bytes),
            )
            return jpeg_bytes
        except ImportError:
            log.warning("screenshot_pillow_not_available")
            return png_bytes
    except Exception as exc:
        log.warning("screenshot_failed", url=mockup_url, error=str(exc)[:200])
        return None


def _capture_sync(mockup_url: str, max_width: int = 500, jpeg_quality: int = 65, timeout_ms: int = 30000):
    if not _is_safe_url(mockup_url):
        log.warning("screenshot_url_rejected", url=mockup_url)
        return None
    try:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            return loop.run_until_complete(
                _capture_async(mockup_url, max_width, jpeg_quality, timeout_ms)
            )
        finally:
            loop.close()
    except Exception as exc:
        log.warning("screenshot_sync_failed", url=mockup_url, error=str(exc)[:200])
        return None


def _upload_to_vps(jpeg_bytes: bytes, lead_id: str) -> str | None:
    """POST the screenshot to login-portal which saves it to the public assets dir."""
    settings = get_settings()
    base_url = (settings.leadgen_login_base_url or "https://login.clientcompass.co.za").rstrip("/")
    api_key = settings.api_key or ""
    if not api_key:
        log.warning("screenshot_upload_no_api_key")
        return None

    # Sanitize lead_id for filename
    safe_id = "".join(c if c.isalnum() or c in "-_" else "_" for c in str(lead_id))[:36]
    filename = f"email_screenshot_{safe_id}.jpg"
    upload_url = f"{base_url}/api/email-assets/upload"

    try:
        req = urllib.request.Request(
            upload_url,
            data=jpeg_bytes,
            method="POST",
            headers={
                "Content-Type": "application/octet-stream",
                "X-Api-Key": api_key,
                "X-Filename": filename,
                # Cloudflare blocks requests without a normal User-Agent. Use a
                # plain browser UA so the WAF lets us through.
                "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            },
        )
        with urllib.request.urlopen(req, timeout=20) as resp:
            body = resp.read().decode("utf-8")
            if resp.status != 200:
                log.warning(
                    "screenshot_upload_failed_status",
                    status=resp.status,
                    body=body[:300],
                )
                return None
            import json as _json
            data = _json.loads(body)
            url = data.get("url")
            if not url:
                log.warning("screenshot_upload_no_url", body=body[:200])
                return None
            log.info(
                "screenshot_uploaded",
                url=url,
                bytes=len(jpeg_bytes),
                lead_id=lead_id,
            )
            return url
    except urllib.error.HTTPError as exc:
        log.warning(
            "screenshot_upload_http_error",
            status=exc.code,
            body=exc.read().decode("utf-8", errors="replace")[:300],
        )
        return None
    except Exception as exc:
        log.warning("screenshot_upload_error", lead_id=lead_id, error=str(exc)[:200])
        return None


def encode_as_data_uri(image_bytes: bytes, mime_type: str = "image/jpeg") -> str | None:
    """Encode raw image bytes as a base64 data URI for inline embedding.
    Used as a fallback when VPS upload fails (Apple Mail / Outlook only)."""
    if not image_bytes:
        return None
    b64 = base64.b64encode(image_bytes).decode("ascii")
    return f"data:{mime_type};base64,{b64}"


def image_size_warning(image_bytes: bytes, max_kb: int = 80) -> bool:
    """Return True if the image is small enough to inline safely (Gmail clips at 102KB)."""
    return len(image_bytes) <= (max_kb * 1024)


def capture_and_upload_screenshot(
    mockup_url: str,
    lead_id: str,
    max_width: int = 500,
    jpeg_quality: int = 65,
    timeout_ms: int = 30000,
) -> tuple[str | None, bool]:
    """Capture screenshot and either upload it (preferred) or inline as data URI.

    Returns:
        (reference, is_url) where:
        - (public_url, True) on successful upload
        - (data_uri, False) on upload failure (works in Apple Mail but not Gmail)
        - (None, False) if capture failed or returned empty
    """
    jpeg_bytes = _capture_sync(mockup_url, max_width, jpeg_quality, timeout_ms)
    if not jpeg_bytes:
        return (None, False)

    # Try VPS upload first (works in all email clients)
    public_url = _upload_to_vps(jpeg_bytes, lead_id)
    if public_url:
        return (public_url, True)

    # Fall back to inline base64 (works in Apple Mail / Outlook; not Gmail)
    if image_size_warning(jpeg_bytes, max_kb=80):
        data_uri = encode_as_data_uri(jpeg_bytes)
        log.info("screenshot_inlined_fallback", lead_id=lead_id, bytes=len(jpeg_bytes))
        return (data_uri, False)
    log.warning("screenshot_too_large_for_inline_fallback", lead_id=lead_id, bytes=len(jpeg_bytes))
    return (None, False)