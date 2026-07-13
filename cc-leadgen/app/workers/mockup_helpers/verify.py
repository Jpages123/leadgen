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
    for img_path in image_urls[:10]:  # cap at 10
        img_url = base + img_path
        try:
            ir = httpx.get(img_url, timeout=10)
            if ir.status_code != 200 or len(ir.content) < 1024:
                image_issues.append({"url": img_url, "status": ir.status_code, "bytes": len(ir.content)})
        except Exception as e:
            image_issues.append({"url": img_url, "error": str(e)[:80]})
    if image_issues:
        issues.append({"name": "broken_images", "ok": False, "items": image_issues[:5]})
    else:
        checks.append({"name": "all_images_ok", "ok": True, "count": len(image_urls)})

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
        "elapsed_ms": int((time.time() - started) * 1000),
    }
