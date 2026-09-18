"""Verify helper — programmatic checks on the live mockup.

Visual verification is done by the LLM via Playwright MCP (browser_take_screenshot).
This helper does programmatic checks only: HTTP status, image bytes, content sanity.
"""
from __future__ import annotations

import re
import time
from urllib.parse import urlparse

import httpx


# Trade-copy phrases that should NOT appear on a non-trades vertical
TRADE_COPY_PHRASES = [
    "emergency call-out",
    "emergency call-out",
    "bathroom renovation",
    "Certificate of Compliance",
    "24/7 emergency response",
    "no-obligation quote",
    "free, no-obligation",
    "no call-out fee",
    "We take pride in every job",
    "registered, insured, and committed",
]


def _is_trades_vertical(vertical: str | None) -> bool:
    if not vertical:
        return False
    v = vertical.lower()
    return any(t in v for t in (
        "plumb", "electric", "construction", "cleaning", "automotive", "car wash",
        "mechanic", "panel beater", "builder", "roofer", "painter",
    ))


def verify(url: str, vertical: str) -> dict:
    """Programmatic checks on the live mockup URL.

    Returns {ok, checks: [...], issues: [...], recommendation: "ship"|"iterate"}
    """
    started = time.time()
    checks: list[dict] = []
    issues: list[dict] = []

    # 1. HTTP reachable + 200
    try:
        r = httpx.get(url, timeout=30, follow_redirects=True)
        if r.status_code == 200:
            checks.append({"name": "http_200", "ok": True, "ms": int(r.elapsed.total_seconds() * 1000)})
        else:
            issues.append({"name": "http_status", "ok": False, "status": r.status_code})
            return {"ok": False, "checks": checks, "issues": issues,
                    "recommendation": "iterate", "elapsed_ms": int((time.time() - started) * 1000)}
    except Exception as e:
        issues.append({"name": "http_reachable", "ok": False, "error": str(e)[:200]})
        return {"ok": False, "checks": checks, "issues": issues,
                "recommendation": "iterate", "elapsed_ms": int((time.time() - started) * 1000)}

    html = r.text
    # 2. Has Playfair Display OR Oswald (font link)
    has_font = ("Playfair+Display" in html) or ("family=Oswald" in html) or ("font-heading" in html)
    checks.append({"name": "has_font", "ok": has_font})

    # 3. Image assets exist on CDN
    image_urls = re.findall(r'src="(/images/[^"]+)"', html)
    image_urls = list(dict.fromkeys(image_urls))  # dedupe, preserve order
    parsed = urlparse(url)
    base = f"{parsed.scheme}://{parsed.netloc}"

    image_issues = []
    placeholder_images = []
    for img_path in image_urls[:10]:  # cap at 10
        img_url = base + img_path
        try:
            ir = httpx.get(img_url, timeout=10)
            if ir.status_code != 200 or len(ir.content) < 1024:
                image_issues.append({"url": img_url, "status": ir.status_code, "bytes": len(ir.content)})
            else:
                # L3: placeholder detection on live deployed images
                try:
                    from PIL import Image as _PILImage
                    from io import BytesIO as _BytesIO
                    img_obj = _PILImage.open(_BytesIO(ir.content)).convert("L")
                    lo, hi = img_obj.getextrema()
                    if (hi - lo) < 40:
                        placeholder_images.append({"url": img_url, "colour_range": hi - lo})
                except Exception:
                    pass
        except Exception as e:
            image_issues.append({"url": img_url, "error": str(e)[:80]})
    if image_issues:
        issues.append({"name": "broken_images", "ok": False, "items": image_issues[:5]})
    else:
        checks.append({"name": "all_images_ok", "ok": True, "count": len(image_urls)})
    if placeholder_images:
        issues.append({"name": "placeholder_images", "ok": False,
                        "items": placeholder_images, "recommendation": "replace with real images"})
    else:
        checks.append({"name": "no_placeholder_images", "ok": True})

    # 3b. Hero/about duplicate check — flagged 2026-08-31: build.py's legacy
    # fallback (and any pool-mode edge case where the pool has only one
    # usable photo) can end up serving the literal same file for both
    # hero.jpg and about.jpg. Byte-identical hero/about reads as broken to
    # a lead even though neither individual image failed the checks above.
    try:
        hero_bytes = httpx.get(base + "/images/hero.jpg", timeout=10).content
        about_bytes = httpx.get(base + "/images/about.jpg", timeout=10).content
        if hero_bytes and about_bytes and hero_bytes == about_bytes:
            issues.append({
                "name": "duplicate_hero_about_image", "ok": False,
                "recommendation": "source a distinct photo for the about slot (mockup_search_stock_images) — do not reuse hero.jpg",
            })
        else:
            checks.append({"name": "hero_about_distinct", "ok": True})
    except Exception:
        pass  # non-fatal — image-level checks above already cover reachability

    # 4. Trade-copy regression check (only for non-trades verticals)
    is_trades = _is_trades_vertical(vertical)
    html_lower = html.lower()
    trade_hits = []
    if not is_trades:
        for phrase in TRADE_COPY_PHRASES:
            if phrase.lower() in html_lower:
                trade_hits.append(phrase)
    if trade_hits:
        issues.append({"name": "trade_copy_on_non_trades", "ok": False,
                        "vertical": vertical, "phrases_found": trade_hits})
    else:
        checks.append({"name": "no_trade_copy_regression", "ok": True})

    # 5. Title sanity (should have the business name, not just placeholder)
    title_match = re.search(r"<title>([^<]+)</title>", html)
    title = title_match.group(1) if title_match else ""
    checks.append({"name": "has_title", "ok": bool(title), "title": title[:80]})

    # 6. Has h1
    h1_match = re.search(r"<h1[^>]*>([^<]+)</h1>", html)
    h1 = h1_match.group(1).strip() if h1_match else ""
    checks.append({"name": "has_h1", "ok": bool(h1), "h1": h1[:80]})

    # Determine recommendation
    recommendation = "iterate" if issues else "ship"
    return {
        "ok": len(issues) == 0,
        "checks": checks,
        "issues": issues,
        "recommendation": recommendation,
        "title": title,
        "h1": h1,
        "placeholder_image_count": len(placeholder_images),
        "elapsed_ms": int((time.time() - started) * 1000),
    }
