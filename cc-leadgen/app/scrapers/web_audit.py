"""Web audit scraper — Playwright-based site analysis.

For each lead with a website URL:
1. Detects CMS platform (Wix, WordPress variants, Joomla, etc.)
2. Extracts copyright year from footer
3. Detects modern-web signals: HTTPS, WhatsApp, JSON-LD, analytics, manifest
4. Detects template leftovers and generic H1s
5. Takes a full-page screenshot
6. Extracts page title, H1, meta description, contact info

All detection is passive (read-only). Uses Playwright sync API so it can run
inside Celery tasks without async complexity.

V2 scoring (2026-07-08):
  - PLATFORM_PITCH_SCORES_V2 stores (age_score, complexity_score) tuples
    so the modern-stack dampener can pick age_score only when the site
    shows investment signals.
  - detect_modern_signals() returns a flags dict that flows through to
    calculate_web_pitch_score() in the worker.
"""
from __future__ import annotations

import os
import re
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse

from playwright.sync_api import sync_playwright, Page, TimeoutError as PlaywrightTimeout
from urllib.parse import urljoin, urlparse

from app.utils.logger import get_logger

log = get_logger(__name__)

# ── Platform fingerprints (from WEB_REVAMP_ENGINE.md) ────────────────────────
# Order matters: more specific variants before generic fallbacks.
PLATFORM_FINGERPRINTS: dict[str, list[str]] = {
    # Global builders
    "wix":                   ["wixsite.com", "static.wixstatic.com", "X-Wix-Published-Version", "_wix_"],
    "squarespace":           ["squarespace.com", "sqsp.net", "squarespace-cdn", "data-sqe"],
    "weebly":                ["weebly.com", "weeblysite.com", "editmysite.com"],
    "godaddy":               ["godaddysites.com", "secureserver.net", "myftpupload.com"],
    "shopify":               ["myshopify.com", "cdn.shopify.com"],

    # WordPress variants (most important — 74% of .za sites)
    "wordpress_divi":        ["et_pb_", "et-db", "DiviBuilder", "/divi/"],
    "wordpress_elementor":   ["elementor-frontend", "elementor/modules", "data-elementor"],
    "wordpress_avada":       ["fusion-builder", "FusionApp", "/avada/"],
    "wordpress_betheme":     ["mfn-", "beTheme", "Muffin Group"],
    "wordpress_generic":     ["wp-content", "wp-includes", "wp-json"],  # fallback

    # Old CMS
    "joomla":                ["joomla", "/components/com_", "Joomla! - Open Source"],
    "drupal":                ["Drupal.settings", "/sites/default/files"],

    # SA-specific / no-CMS signals
    "afrihost_sitebuilder":  ["sitebuilder.afrihost", "afrihost.com/sitebuilder"],
    "yola":                  ["yola.com", "yolasitecreator"],
    "google_sites":          ["sites.google.com"],
}


# ── V2 platform scoring (2026-07-08) ────────────────────────────────────────
# Splits each platform into two components:
#   age_score (0-15): how old / unsupported the platform is
#   complexity_score (0-15): how hard for the owner to migrate off
#
# Total per platform caps at 30, same as V1. The split matters because the
# modern-stack dampener (Tier 2b) zeroes out age_score when a site shows
# 4+ investment signals (HTTPS + JSON-LD + analytics + WhatsApp).
# See calculate_web_pitch_score() in app/workers/web_audit.py for usage.
PLATFORM_PITCH_SCORES_V2: dict[str, tuple[int, int]] = {
    #                       (age, complexity)
    "wix":                   (10, 15),  # recent Wix = fresh, but lock-in
    "godaddy":               ( 5, 15),  # GoDaddy Builder 8.0 = recent, lock-in
    "godaddy_legacy":        (25, 20),  # pre-Builder-8 GoDaddy = stale + lock-in
    "weebly":                (20, 10),
    "joomla":                (25, 10),  # ancient + semi-maintainable
    "squarespace":           ( 5, 10),  # recent + some lock-in
    "wordpress_divi":        (20,  5),  # old builder + WP-easy to migrate
    "wordpress_elementor":   (10,  5),  # recent builder + WP-easy
    "wordpress_avada":       (15,  5),
    "wordpress_betheme":     (15,  5),
    "wordpress_generic":     (15,  5),
    "afrihost_sitebuilder":  (30, 25),  # maximum pain on both axes
    "yola":                  (30, 20),  # ancient + lock-in
    "google_sites":          (10, 15),
    "static_html":           (25, 20),  # totally stuck
    "shopify":               ( 0,  0),  # skip — not our market
    "drupal":                ( 0,  0),  # skip — usually corporates
}

# Backward-compat V1 view (sum of V2 components). Kept so any external
# consumer of PLATFORM_PITCH_SCORES still gets a number.
PLATFORM_PITCH_SCORES: dict[str, int] = {
    p: age + comp for p, (age, comp) in PLATFORM_PITCH_SCORES_V2.items()
}

# Map the "godaddy" fingerprint to a separate "godaddy_legacy" bucket when
# the site shows no GoDaddy-Builder-8 indicators. Lets GoDaddy Builder 8.0
# sites (recent, well-supported) score much lower than legacy GoDaddy sites.
GODADDY_BUILDER_8_FINGERPRINTS = [
    "GoDaddy Website Builder",       # meta generator string for Builder 8
    "Starfield Technologies",        # GoDaddy parent company
    "/blobby/go/static/radpack/",    # Builder 8 asset CDN
    "img1.wsimg.com",               # Builder 8 image CDN
]


@dataclass
class WebAuditResult:
    url: str
    platform: Optional[str] = None
    platform_pitch_score: int = 0
    copyright_year: Optional[int] = None
    screenshot_path: Optional[str] = None
    title: Optional[str] = None
    h1: Optional[str] = None
    meta_description: Optional[str] = None
    phone: Optional[str] = None
    email: Optional[str] = None
    error: Optional[str] = None
    broken_elements: list[str] = field(default_factory=list)

    # ── Modern-web signals (added 2026-07-08 — migration 005) ──────────
    # Each flag is a cheap boolean derived from the existing Playwright
    # scrape + PageSpeed response. Drives Tier-1 bonuses and Tier-2b
    # modern-stack dampener in calculate_web_pitch_score().
    is_https: bool = False
    has_whatsapp: bool = False
    has_jsonld: bool = False
    has_analytics: bool = False
    has_manifest: bool = False
    h1_text: Optional[str] = None
    has_unedited_template: bool = False
    # Convenience: rendered H1 text we actually saw (post-trim). When this
    # matches GENERIC_H1_PATTERNS we add a Tier-1 bonus in the worker.

    # ── Personalization assets (added 2026-07-04) ─────────────────────────
    # Populated by asset extraction during audit_website(). Used by the mockup
    # generator to produce a personalized preview (logo, hero image, brand
    # color) instead of falling back to vertical defaults.

    # URL quality check: True if the URL points to a booking platform,
    # social profile, or directory (e.g. fresha.com, tiktok.com) instead
    # of the business's own website. Mockup generation skips these leads.
    needs_url_review: bool = False
    url_quality_issue: Optional[str] = None

    # Logo: smallest <img> near <header> on the page (intentional brand mark)
    logo_url: Optional[str] = None
    logo_path: Optional[str] = None

    # Hero image: largest <img> on the home page (by natural pixel area)
    hero_url: Optional[str] = None
    hero_path: Optional[str] = None

    # Gallery images: pulled from /gallery, /work, /portfolio pages if found
    gallery_urls: list[str] = field(default_factory=list)
    gallery_paths: list[str] = field(default_factory=list)

    # Brand color: derived from logo image (most-saturated pixel) or from
    # CSS computedStyle of prominent elements if no logo. Hex like "#1e9be8".
    brand_color_hex: Optional[str] = None


# ── URL quality check ────────────────────────────────────────────────────────
# Google Places sometimes returns URLs that point to booking platforms, social
# profiles, or directories instead of the business's own website. Mockups
# generated from these would be wrong (mocking up Fresha's brand instead of
# the nail salon's). We flag such leads and skip mockup generation.

_URL_QUALITY_REJECT = [
    # Booking / scheduling platforms
    ("fresha.com",      "fresha"),
    ("booksy.com",      "booksy"),
    ("book.ink",        "book_ink"),
    ("mytime.com",      "mytime"),
    ("styleseat.com",   "styleseat"),
    ("vagaro.com",      "vagaro"),
    ("squareup.com",    "square"),
    ("setmore.com",     "setmore"),
    ("simplybook.me",   "simplybook"),
    ("salonbooking.com","salonbooking"),
    ("booker.com",      "booker"),
    # Social profiles (these are pages about the business, not the business site)
    ("tiktok.com",      "tiktok"),
    ("facebook.com",    "facebook"),
    ("instagram.com",   "instagram"),
    ("twitter.com",     "twitter"),
    ("x.com",           "x_twitter"),
    ("linkedin.com",    "linkedin"),
    ("pinterest.com",   "pinterest"),
    ("youtube.com",     "youtube"),
    # Travel / hospitality
    ("booking.com",     "booking_com"),
    ("tripadvisor.com", "tripadvisor"),
    ("airbnb.com",      "airbnb"),
    # Directories / aggregators
    ("yelp.com",        "yelp"),
    ("yell.com",        "yell"),
    ("yelp.co.za",      "yelp"),
    ("snupit.co.za",    "snupit"),
    ("yellowpages.co.za","yellowpages"),
    ("hotfrog.co.za",   "hotfrog"),
    ("africanadvice.com","africanadvice"),
    ("safoodreview.com","safoodreview"),
    # Marketplaces (not their own store)
    ("takealot.com",    "takealot"),
    ("shopify.com",     "shopify_marketplace"),
    ("etsy.com",        "etsy"),
]


def check_url_quality(url: str) -> tuple[bool, Optional[str]]:
    """Return (is_low_quality, reason) for a website URL.

    is_low_quality=True means the URL points to a platform/profile/directory
    rather than the business's own site. Mockup generation should be skipped.
    """
    if not url:
        return False, None
    url_lower = url.lower()
    for domain, reason in _URL_QUALITY_REJECT:
        if domain in url_lower:
            return True, reason
    return False, None


def detect_platform(html: str, headers: dict[str, str]) -> Optional[str]:
    """Detect CMS platform from page HTML + response headers.

    Returns the fingerprint match (e.g. 'godaddy', 'wordpress_divi') or None.
    Caller should map to 'static_html' if None.

    GoDaddy quirk: when the site uses GoDaddy Website Builder 8.0, we still
    return 'godaddy' — the worker checks GODADDY_BUILDER_8_FINGERPRINTS via
    detect_godaddy_legacy() and downgrades to 'godaddy_legacy' if appropriate.
    """
    combined = html + " " + " ".join(f"{k}: {v}" for k, v in headers.items())

    for platform, fingerprints in PLATFORM_FINGERPRINTS.items():
        for fp in fingerprints:
            if fp in combined:
                return platform

    return None


def is_godaddy_builder_8(html: str, headers: dict[str, str]) -> bool:
    """True when the GoDaddy site uses the current Website Builder 8.0.

    Builder 8 has the modern radpack/webpack JS pipeline + Starfield
    meta generator. Sites on legacy GoDaddy SiteBuilder lack these.
    """
    combined = html + " " + " ".join(f"{k}: {v}" for k, v in headers.items())
    return any(fp in combined for fp in GODADDY_BUILDER_8_FINGERPRINTS)


def extract_copyright_year(html: str) -> Optional[int]:
    """Extract copyright year from page HTML footer.

    Matches: © 2018, Copyright 2016, &copy; 2020
    Returns the year as int, or None if not found.
    """
    match = re.search(r'[©&](?:copy;)?\s*(\d{4})', html)
    if match:
        year = int(match.group(1))
        if 2000 <= year <= 2030:  # sanity check
            return year

    # Also try plain text "Copyright YYYY"
    match = re.search(r'[Cc]opyright\s+(\d{4})', html)
    if match:
        year = int(match.group(1))
        if 2000 <= year <= 2030:
            return year

    return None


def extract_contact_info(html: str) -> tuple[Optional[str], Optional[str]]:
    """Extract phone and email from page HTML."""
    phone = None
    email = None

    # Phone: tel: links or common SA number patterns
    tel_match = re.search(r'tel:([+\d\s\-()]{7,15})', html)
    if tel_match:
        phone = tel_match.group(1).strip()

    # Email: mailto: links
    mail_match = re.search(r'mailto:([a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,})', html)
    if mail_match:
        email = mail_match.group(1).strip()

    return phone, email


# ── Modern-web signal detection (2026-07-08) ────────────────────────────────
# Each function takes a Playwright Page + the page HTML and returns a bool.
# All cheap — no extra network calls. Page must be loaded with the same
# context that produced `html` (i.e. evaluate() runs against the live DOM).

# WhatsApp: detect wa.me/<num>, web.whatsapp.com/send, api.whatsapp.com, or
# whatsapp:// scheme. These are the dominant SA business click-to-chat patterns.
_WHATSAPP_PATTERNS = [
    # URL-based click-to-chat patterns (the real WhatsApp CTAs we want).
    # We require href= wrapping to avoid counting loose "WhatsApp" text
    # mentions (e.g. a footer saying "we use WhatsApp" without a link).
    re.compile(r'href=["\'][^"\']*wa\.me/\d+', re.I),
    re.compile(r'href=["\'][^"\']*web\.whatsapp\.com/send', re.I),
    re.compile(r'href=["\'][^"\']*api\.whatsapp\.com/send', re.I),
    re.compile(r'href=["\'][^"\']*whatsapp://', re.I),
]

# Analytics: gtag.js (GA4), analytics.js (UA), Google Tag Manager, FB Pixel
_ANALYTICS_PATTERNS = [
    re.compile(r'google-analytics\.com/analytics\.js', re.I),
    re.compile(r'google-analytics\.com/ga\.js', re.I),
    re.compile(r'googletagmanager\.com/gtag', re.I),
    re.compile(r'googletagmanager\.com/gtm\.js', re.I),
    re.compile(r'connect\.facebook\.net[^"\']*fbevents\.js', re.I),
    re.compile(r'connect\.facebook\.net[^"\']*signals/config', re.I),
    re.compile(r'hotjar\.com', re.I),
    re.compile(r'clarity\.ms', re.I),
]


def detect_https(headers: dict[str, str], url: str) -> bool:
    """True if the site was served over HTTPS.

    Two ways to check: response headers (set on the navigation response)
    or the URL itself. Both should agree; we check URL first as a fast path.
    """
    if url.startswith("https://"):
        return True
    # Some scrapers normalise URLs — check header as fallback
    return headers.get("X-Forwarded-Proto", "").lower() == "https"


def detect_whatsapp(html: str) -> bool:
    """True if the page has any WhatsApp contact link or visible mention."""
    return any(p.search(html) for p in _WHATSAPP_PATTERNS)


def detect_jsonld(html: str) -> bool:
    """True if the page has at least one JSON-LD structured-data block.

    Cheap text check — JSON-LD is always a <script type="application/ld+json">.
    """
    return "application/ld+json" in html


def detect_analytics(html: str) -> bool:
    """True if the page loads any known analytics script (GA / GTM / FB / HJ)."""
    return any(p.search(html) for p in _ANALYTICS_PATTERNS)


def detect_manifest(html: str) -> bool:
    """True if the page references a PWA web manifest."""
    return bool(
        re.search(r'<link[^>]+rel=["\']manifest["\']', html, re.I)
        or re.search(r'<link[^>]+rel=["\']manifest["\']', html, re.I)
    )


# Generic-H1 patterns: text that screams "template not customised" or
# "broken SEO". Match is case-insensitive and trimmed.
GENERIC_H1_PATTERNS = [
    "about us",
    "about",
    "home",
    "welcome",
    "welcome to",
    "home page",
    "lorem ipsum",
    "placeholder",
    "untitled",
    "coming soon",
    "under construction",
    "tagline goes here",
    "add a tagline",
    "enter your text",
    "sample text",
    "edit me",
]


def is_generic_h1(h1_text: Optional[str]) -> bool:
    """True if the H1 is a known template default or SEO-broken text.

    These patterns all signal the owner has never invested in on-page SEO.
    """
    if not h1_text:
        return False
    h1 = h1_text.strip().lower()
    if not h1:
        return False
    if len(h1) > 80:  # Real H1s are short — if it's longer, it's body copy
        return False
    return any(p in h1 for p in GENERIC_H1_PATTERNS)


# Template-leftover keywords: copy from default templates that was never
# replaced. Lower-case substring match against concatenated nav + h1 + h2s.
TEMPLATE_LEFTOVER_KEYWORDS = [
    "lorem ipsum",
    "edit me",
    "placeholder",
    "click to edit",
    "sample text",
    "tagline goes here",
    "add a tagline",
    "enter your text",
    # Known GoDaddy / Wix template fragments
    "our dresses",
    "join our awesome team",
    "join our team!!",
    "my account",
    "create account",
    "filler@",
]


def detect_unedited_template(html: str, nav_text: str, h1: Optional[str], h2s: list[str]) -> bool:
    """True if the page contains known template-leftover copy.

    Search strategy:
      1. Look for the most damning substrings ("lorem ipsum", "edit me") anywhere
         in the rendered HTML — these should never appear on a real site.
      2. Look for GoDaddy / Wix template defaults in the nav text only
         (these can appear in legit footers via copy-paste, so we restrict
         to navigation to avoid false positives).
      3. Look for obvious placeholders in the H1.

    Returns True if any of the above match.
    """
    html_lower = html.lower()
    nav_lower = (nav_text or "").lower()
    h1_lower = (h1 or "").lower()

    # Tier A: high-confidence substrings — anywhere in HTML
    high_confidence = [
        "lorem ipsum", "edit me", "click to edit",
        "tagline goes here", "add a tagline", "enter your text",
        "filler@",
    ]
    if any(kw in html_lower for kw in high_confidence):
        return True

    # Tier B: template defaults — only in nav (avoids footer false positives)
    nav_only = ["our dresses", "join our awesome team", "join our team!!"]
    if any(kw in nav_lower for kw in nav_only):
        return True

    # Tier C: placeholders in the H1
    h1_placeholders = ["placeholder", "untitled", "sample text"]
    if any(kw in h1_lower for kw in h1_placeholders):
        return True

    return False


def _slug_from_url(url: str) -> str:
    """Convert URL to a filesystem-safe slug."""
    parsed = urlparse(url)
    slug = parsed.netloc.replace("www.", "").replace(".", "-")
    return slug or "unknown"


# ── Personalization asset extraction (added 2026-07-04) ─────────────────────
# These helpers are used by audit_website() to populate logo/hero/gallery/brand_color
# fields. They are intentionally separate from audit_website() so they can be
# unit-tested and evolved without touching the main audit flow.


def _extract_logo_url(page: Page, base_url: str) -> Optional[str]:
    """Find the most likely logo image on the page.

    Heuristics (in priority order):
      1. <img> inside <header> or <nav> with "logo" in src/alt/class
      2. <a href="/"> containing an <img> (typical logo-link pattern)
      3. Smallest <img> in the first 600px of the page (logos are usually small)

    Returns absolute URL or None.
    """
    try:
        candidates: list[tuple[str, int]] = []  # (url, area_score)

        # Strategy 1: explicit logo images
        for sel in [
            "header img[alt*='logo' i]",
            "header img[src*='logo' i]",
            "header img[class*='logo' i]",
            "nav img[alt*='logo' i]",
            "nav img[src*='logo' i]",
            "img[class*='logo' i]",
            "img[id*='logo' i]",
        ]:
            for el in page.query_selector_all(sel):
                src = el.get_attribute("src") or ""
                if not src or src.startswith("data:"):
                    continue
                abs_url = urljoin(base_url, src)
                w = el.get_attribute("width")
                # logos are typically < 400px wide; prefer smaller
                w_int = int(w) if (w and w.isdigit()) else 200
                candidates.append((abs_url, w_int))

        # Strategy 2: anchor wrapping logo
        for sel in ["header a img", "nav a img"]:
            for el in page.query_selector_all(sel):
                src = el.get_attribute("src") or ""
                if not src or src.startswith("data:"):
                    continue
                abs_url = urljoin(base_url, src)
                w = el.get_attribute("width")
                w_int = int(w) if (w and w.isdigit()) else 200
                candidates.append((abs_url, w_int))

        if candidates:
            # Return the smallest (logos are intentionally small)
            candidates.sort(key=lambda x: x[1])
            return candidates[0][0]

        # Strategy 3: smallest header image by inferred size
        try:
            sizes = page.evaluate(
                """() => {
                    const imgs = Array.from(document.querySelectorAll('header img, nav img')).slice(0, 10);
                    return imgs.map(img => ({
                        src: img.currentSrc || img.src,
                        nw: img.naturalWidth || img.width || 9999,
                        nh: img.naturalHeight || img.height || 9999,
                    }));
                }"""
            )
            scored = [
                (s["src"], s["nw"] * s["nh"])
                for s in sizes
                if s.get("src") and not s["src"].startswith("data:")
            ]
            if scored:
                scored.sort(key=lambda x: x[1])
                return urljoin(base_url, scored[0][0])
        except Exception:
            pass

    except Exception as exc:
        log.warning("extract_logo_failed", url=base_url, error=str(exc)[:200])
    return None


def _extract_hero_url(page: Page, base_url: str) -> Optional[str]:
    """Find the largest hero image on the home page.

    Heuristics:
      - All <img> tags with naturalWidth >= 400 and naturalHeight >= 250
      - Pick the one with the largest natural pixel area
      - Skip logos, icons, and tracking pixels
    """
    try:
        sizes = page.evaluate(
            """() => {
                const imgs = Array.from(document.querySelectorAll('img')).slice(0, 50);
                return imgs
                    .map(img => ({
                        src: img.currentSrc || img.src,
                        nw: img.naturalWidth || img.width || 0,
                        nh: img.naturalHeight || img.height || 0,
                        alt: (img.alt || '').toLowerCase(),
                        cls: (img.className || '').toLowerCase(),
                    }))
                    .filter(s => s.src && !s.src.startsWith('data:'))
                    .filter(s => s.nw >= 400 && s.nh >= 250);
            }"""
        )
        scored = [(s["src"], s["nw"] * s["nh"]) for s in sizes]
        # Prefer images that aren't logos
        scored.sort(key=lambda x: (0 if "logo" in x[0].lower() else 1, -x[1]))
        if scored:
            return urljoin(base_url, scored[0][0])
    except Exception as exc:
        log.warning("extract_hero_failed", url=base_url, error=str(exc)[:200])
    return None


def _extract_gallery_urls(page: Page, base_url: str, max_n: int = 4) -> list[str]:
    """Find image candidates on this page (used by gallery probe).

    Returns up to max_n absolute URLs of mid-sized images (likely photos, not
    logos or icons). Order is roughly by descending pixel area.
    """
    try:
        sizes = page.evaluate(
            """() => {
                const imgs = Array.from(document.querySelectorAll('img')).slice(0, 100);
                return imgs
                    .map(img => ({
                        src: img.currentSrc || img.src,
                        nw: img.naturalWidth || img.width || 0,
                        nh: img.naturalHeight || img.height || 0,
                        alt: (img.alt || '').toLowerCase(),
                    }))
                    .filter(s => s.src && !s.src.startsWith('data:'))
                    .filter(s => s.nw >= 250 && s.nh >= 200)
                    .filter(s => !/logo|icon|avatar|sprite/i.test(s.alt));
            }"""
        )
        scored = sorted(sizes, key=lambda s: -(s["nw"] * s["nh"]))
        return [urljoin(base_url, s["src"]) for s in scored[:max_n]]
    except Exception as exc:
        log.warning("extract_gallery_failed", url=base_url, error=str(exc)[:200])
    return []


def _extract_brand_color_from_css(page: Page) -> Optional[str]:
    """Sample computed background-color from prominent elements. Skip neutrals.

    Returns hex string like "#1e9be8" or None.
    """
    try:
        colors = page.evaluate(
            """() => {
                const sel = 'a, button, [class*="btn"], header, nav, [class*="cta"]';
                const out = new Set();
                document.querySelectorAll(sel).forEach(el => {
                    const bg = getComputedStyle(el).backgroundColor;
                    const fg = getComputedStyle(el).color;
                    [bg, fg].forEach(c => out.add(c));
                });
                return Array.from(out);
            }"""
        )
        best = _pick_most_saturated_hex(colors)
        return best
    except Exception as exc:
        log.warning("extract_color_from_css_failed", error=str(exc)[:200])
    return None


def _pick_most_saturated_hex(rgb_strings: list[str]) -> Optional[str]:
    """From a list of CSS rgb()/rgba() strings, pick the most saturated one
    that's not too close to white/black/grey. Returns hex.
    """
    best: Optional[tuple[str, float]] = None
    for s in rgb_strings:
        if not isinstance(s, str):
            continue
        # Parse "rgb(r, g, b)" or "rgba(r, g, b, a)"
        s = s.strip()
        if not s.startswith("rgb"):
            continue
        try:
            nums = s[s.find("(") + 1 : s.find(")")].split(",")
            r = int(nums[0].strip())
            g = int(nums[1].strip())
            b = int(nums[2].strip())
        except Exception:
            continue
        mx, mn = max(r, g, b), min(r, g, b)
        # Skip near-white (all 3 channels bright) / near-black (all dark) / grey
        if mn > 230:        # all channels > 230 = near-white
            continue
        if mx < 25:         # all channels < 25 = near-black
            continue
        if mx - mn < 30:    # greyish — chroma too low to be a brand color
            continue
        # Saturation score: chroma (mx - mn) + bonus for not being too light
        chroma = mx - mn
        lightness_penalty = max(0, (mx - 180) * 0.5)
        score = chroma - lightness_penalty
        hex_str = f"#{r:02x}{g:02x}{b:02x}"
        if best is None or score > best[1]:
            best = (hex_str, score)
    return best[0] if best else None


def _extract_brand_color_from_logo(logo_path: str) -> Optional[str]:
    """From a downloaded logo image, return the most saturated pixel as hex.

    Skips near-white / near-black / grey. Falls back to None if logo can't be read.
    """
    try:
        from PIL import Image
        img = Image.open(logo_path).convert("RGB")
        # Downsample for speed — 100x100 is enough for color extraction
        img_small = img.resize((100, 100), Image.NEAREST)
        # Collect pixels as rgb list
        pixels = list(img_small.getdata())
        # Build rgb strings for the helper
        rgb_strings = [f"rgb({r},{g},{b})" for (r, g, b) in pixels]
        return _pick_most_saturated_hex(rgb_strings)
    except Exception as exc:
        log.warning("extract_color_from_logo_failed", path=logo_path, error=str(exc)[:200])
        return None


def _download_image(url: str, dest_path: str, timeout: float = 8.0) -> bool:
    """Download an image via httpx. Returns True on success."""
    try:
        import httpx
        with httpx.Client(timeout=timeout, follow_redirects=True) as client:
            resp = client.get(url, headers={
                "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/124.0.0.0 Safari/537.36 ClientCompassAudit/1.0",
                "Accept": "image/*,*/*",
            })
            resp.raise_for_status()
            Path(dest_path).parent.mkdir(parents=True, exist_ok=True)
            with open(dest_path, "wb") as f:
                f.write(resp.content)
            return True
    except Exception as exc:
        log.warning("image_download_failed", url=url, error=str(exc)[:200])
        return False


def _guess_extension(url: str, content_type: str = "") -> str:
    """Best-guess file extension for a downloaded image URL."""
    path = urlparse(url).path.lower()
    for ext in (".jpg", ".jpeg", ".png", ".webp", ".gif", ".svg"):
        if path.endswith(ext):
            return ext
    if "png" in content_type:
        return ".png"
    if "webp" in content_type:
        return ".webp"
    return ".jpg"


_GALLERY_PATHS = ["/gallery", "/work", "/portfolio", "/our-work", "/projects"]


def _probe_gallery_pages(
    page: Page, base_url: str, screenshot_dir: str, slug: str
) -> list[tuple[str, str]]:
    """Visit common gallery URLs and collect image URLs + local paths.

    Returns list of (absolute_url, local_path) for the best images found.
    """
    found: list[tuple[str, str]] = []
    tried = set()
    parsed = urlparse(base_url)
    origin = f"{parsed.scheme}://{parsed.netloc}"

    for path in _GALLERY_PATHS:
        url = origin + path
        if url in tried:
            continue
        tried.add(url)
        try:
            resp = page.goto(url, timeout=8000, wait_until="domcontentloaded")
            if not resp or resp.status >= 400:
                continue
            page.wait_for_timeout(800)
            urls = _extract_gallery_urls(page, url, max_n=4)
            for u in urls:
                ext = _guess_extension(u)
                local = os.path.join(screenshot_dir, f"{slug}-gallery-{len(found)+1}{ext}")
                if _download_image(u, local):
                    found.append((u, local))
                    if len(found) >= 4:
                        return found
        except Exception:
            continue
    return found


def audit_website(
    url: str,
    screenshot_dir: str | None = None,
    timeout_ms: int = 20000,
) -> WebAuditResult:
    """Run a full web audit on a URL.

    Args:
        url: Full URL to audit (e.g. 'https://dgfplumbing.co.za')
        screenshot_dir: Directory to save screenshot. Defaults to system temp.
        timeout_ms: Page load timeout in milliseconds.

    Returns:
        WebAuditResult with all extracted fields (including V2 modern-web signals).
    """
    if not url.startswith(("http://", "https://")):
        url = f"https://{url}"

    result = WebAuditResult(url=url)
    slug = _slug_from_url(url)

    if screenshot_dir is None:
        screenshot_dir = os.path.join(tempfile.gettempdir(), "cc_audits")
    Path(screenshot_dir).mkdir(parents=True, exist_ok=True)

    screenshot_path = os.path.join(screenshot_dir, f"{slug}.jpg")

    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True)
            context = browser.new_context(
                viewport={"width": 1280, "height": 900},
                user_agent=(
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/124.0.0.0 Safari/537.36"
                ),
            )
            page = context.new_page()

            # Capture response headers from the main navigation
            response_headers: dict[str, str] = {}

            def on_response(response):
                nonlocal response_headers
                if response.url == page.url or url in response.url:
                    try:
                        response_headers.update(response.headers)
                    except Exception:
                        pass

            page.on("response", on_response)

            try:
                page.goto(url, timeout=timeout_ms, wait_until="domcontentloaded")
                # Brief wait for JS to render (Wix/Elementor need this)
                page.wait_for_timeout(2000)
            except PlaywrightTimeout:
                result.error = f"Page load timeout ({timeout_ms}ms)"
                browser.close()
                return result

            # Get full HTML
            html = page.content()

            # ── HTTPS check (cheap — URL + header) ──────────────────────
            result.is_https = detect_https(response_headers, url)

            # ── Platform detection ──────────────────────────────────────
            raw_platform = detect_platform(html, response_headers)
            # GoDaddy quirk: distinguish Builder 8.0 (recent, well-supported)
            # from legacy GoDaddy sites. Builder 8.0 falls back to a much
            # lower age_score via PLATFORM_PITCH_SCORES_V2.
            if raw_platform == "godaddy" and is_godaddy_builder_8(html, response_headers):
                result.platform = "godaddy"  # keep 'godaddy' as the bucket key
                result.platform_pitch_score = PLATFORM_PITCH_SCORES_V2["godaddy"][0] + PLATFORM_PITCH_SCORES_V2["godaddy"][1]
            elif raw_platform:
                result.platform = raw_platform
                age, comp = PLATFORM_PITCH_SCORES_V2.get(raw_platform, (0, 0))
                result.platform_pitch_score = age + comp
            else:
                result.platform = "static_html"
                age, comp = PLATFORM_PITCH_SCORES_V2["static_html"]
                result.platform_pitch_score = age + comp

            # ── Copyright year ──────────────────────────────────────────
            result.copyright_year = extract_copyright_year(html)

            # ── Page metadata ───────────────────────────────────────────
            try:
                result.title = page.title() or None
            except Exception:
                pass

            try:
                h1 = page.query_selector("h1")
                h1_text = h1.inner_text().strip() if h1 else None
                result.h1 = h1_text
                result.h1_text = h1_text
            except Exception:
                pass

            try:
                meta = page.query_selector('meta[name="description"]')
                result.meta_description = meta.get_attribute("content") if meta else None
            except Exception:
                pass

            # ── Contact info ────────────────────────────────────────────
            result.phone, result.email = extract_contact_info(html)

            # ── Modern-web signals (migration 005) ─────────────────────
            # Cheap text/header checks — no extra network calls.
            result.has_whatsapp = detect_whatsapp(html)
            result.has_jsonld   = detect_jsonld(html)
            result.has_analytics = detect_analytics(html)
            result.has_manifest = detect_manifest(html)

            # Generic H1 check (cheap string match against known patterns)
            # Stored as a derived signal via has_unedited_template below.

            # Template-leftover detection — needs nav + h2 text from DOM
            try:
                nav_text, h2_list = page.evaluate(
                    """() => {
                        const navEls = Array.from(document.querySelectorAll('nav a, header a'))
                            .map(a => a.textContent.trim())
                            .filter(Boolean);
                        const h2List = Array.from(document.querySelectorAll('h2'))
                            .map(h => h.textContent.trim());
                        return [navEls.join(' '), h2List];
                    }"""
                )
            except Exception:
                nav_text, h2_list = "", []
            result.has_unedited_template = detect_unedited_template(
                html=html,
                nav_text=nav_text or "",
                h1=result.h1_text,
                h2s=h2_list or [],
            )

            # ── Screenshot ──────────────────────────────────────────────
            try:
                page.screenshot(path=screenshot_path, full_page=False, type="jpeg", quality=80)
                result.screenshot_path = screenshot_path
            except Exception as exc:
                log.warning("web_audit_screenshot_failed", url=url, error=str(exc))

            # ── Personalization assets (logo / hero / brand color / gallery) ──
            try:
                # Logo
                logo_url = _extract_logo_url(page, url)
                if logo_url:
                    result.logo_url = logo_url
                    ext = _guess_extension(logo_url)
                    logo_path = os.path.join(screenshot_dir, f"{slug}-logo{ext}")
                    if _download_image(logo_url, logo_path):
                        result.logo_path = logo_path

                # Hero image
                hero_url = _extract_hero_url(page, url)
                if hero_url:
                    result.hero_url = hero_url
                    ext = _guess_extension(hero_url)
                    hero_path = os.path.join(screenshot_dir, f"{slug}-hero{ext}")
                    if _download_image(hero_url, hero_path):
                        result.hero_path = hero_path

                # Brand color: prefer logo image, fall back to CSS sampling
                if result.logo_path and os.path.exists(result.logo_path):
                    logo_color = _extract_brand_color_from_logo(result.logo_path)
                    if logo_color:
                        result.brand_color_hex = logo_color
                if not result.brand_color_hex:
                    css_color = _extract_brand_color_from_css(page)
                    if css_color:
                        result.brand_color_hex = css_color

                # Gallery (probes /gallery, /work, /portfolio if present)
                gallery_pairs = _probe_gallery_pages(page, url, screenshot_dir, slug)
                result.gallery_urls = [u for (u, _) in gallery_pairs]
                result.gallery_paths = [p for (_, p) in gallery_pairs]
            except Exception as exc:
                log.warning("asset_extraction_failed", url=url, error=str(exc)[:200])

            # ── Broken element detection ────────────────────────────────
            broken = []
            # Check for broken Google Maps iframes
            maps_iframes = page.query_selector_all('iframe[src*="google.com/maps"]')
            for iframe in maps_iframes:
                src = iframe.get_attribute("src") or ""
                if not src or "YOUR_API_KEY" in src:
                    broken.append("broken_maps_embed")
                    break

            result.broken_elements = broken

            browser.close()

    except Exception as exc:
        result.error = str(exc)[:300]
        log.error("web_audit_failed", url=url, error=str(exc))

    log.info(
        "web_audit_done",
        url=url,
        platform=result.platform,
        copyright_year=result.copyright_year,
        pitch_score=result.platform_pitch_score,
        is_https=result.is_https,
        has_whatsapp=result.has_whatsapp,
        has_jsonld=result.has_jsonld,
        has_analytics=result.has_analytics,
        has_manifest=result.has_manifest,
        has_unedited_template=result.has_unedited_template,
        error=result.error,
    )

    return result