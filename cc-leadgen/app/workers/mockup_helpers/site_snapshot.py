"""site_snapshot.py — synchronous pre-analysis of a lead's live website.

Runs BEFORE the Pi subprocess spawns so Pi has factual copy + image data
in its context from the very first token.

Timeout: 20s total (network-bound — SA sites can be slow).
Fallback: if anything fails, returns a SiteSnapshot with error set.
Pi handles the error gracefully by falling back to DB data only.
"""
from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional
from urllib.parse import urljoin, urlparse

import httpx
from PIL import Image as PILImage
from io import BytesIO

from app.utils.logger import get_logger
from app.utils.mockup_build import mockup_project_dir

log = get_logger(__name__)

SNAPSHOT_TIMEOUT_S = 20
IMAGE_DOWNLOAD_TIMEOUT_S = 8
MAX_GALLERY_IMAGES = 4
PLACEHOLDER_COLOUR_RANGE_THRESHOLD = 40  # px range in greyscale — below = solid colour
MIN_IMAGE_BYTES = 2048  # anything smaller is almost certainly a placeholder/icon


@dataclass
class ImageAsset:
    url: str
    local_path: Optional[str] = None
    width: Optional[int] = None
    height: Optional[int] = None
    bytes: int = 0
    is_placeholder: bool = False

    def to_xml(self, tag: str) -> str:
        attrs = f'url="{self.url}" bytes="{self.bytes}" placeholder="{str(self.is_placeholder).lower()}"'
        if self.local_path:
            attrs += f' local="{self.local_path}"'
        if self.width:
            attrs += f' width="{self.width}" height="{self.height}"'
        return f"<{tag} {attrs}/>"


@dataclass
class SiteSnapshot:
    url: str
    page_title: Optional[str] = None
    h1: Optional[str] = None
    meta_description: Optional[str] = None
    services: list[str] = field(default_factory=list)
    phone: Optional[str] = None
    email: Optional[str] = None
    testimonials: list[str] = field(default_factory=list)
    google_rating: Optional[float] = None
    review_count: Optional[int] = None
    hero: Optional[ImageAsset] = None
    logo: Optional[ImageAsset] = None
    gallery: list[ImageAsset] = field(default_factory=list)
    screenshot_path: Optional[str] = None
    error: Optional[str] = None
    scraped_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())


# ── HTML extraction helpers ───────────────────────────────────────────────────

_PHONE_RE = re.compile(
    r"(?:\+27|0)[6-8]\d[\s\-]?\d{3}[\s\-]?\d{4}"  # SA mobile (+27/0 + 6xx/7xx/8xx)
    r"|(?:\+27|0)[1-5]\d[\s\-]?\d{3}[\s\-]?\d{4}",  # SA landline (+27/0 + 1xx-5xx)
    re.ASCII,
)
_EMAIL_RE = re.compile(r"[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}")
_RATING_RE = re.compile(r"(\d+\.?\d*)\s*/\s*5|rated?\s+(\d+\.?\d*)\s+out", re.IGNORECASE)
_REVIEW_COUNT_RE = re.compile(r"(\d+)\s+(?:reviews?|ratings?)", re.IGNORECASE)


def _strip_tags(html: str) -> str:
    return re.sub(r"<[^>]+>", " ", html)


def _extract_text_block(html: str, tag: str, attrs_re: str = "") -> list[str]:
    pattern = rf"<{tag}[^>]*{attrs_re}[^>]*>(.*?)</{tag}>"
    return [_strip_tags(m).strip() for m in re.findall(pattern, html, re.DOTALL | re.IGNORECASE)]


def _extract_meta(html: str, name: str) -> Optional[str]:
    m = re.search(
        rf'<meta[^>]+(?:name|property)=["\'](?:og:)?{name}["\'][^>]+content=["\']([^"\']+)["\']',
        html, re.IGNORECASE,
    )
    if not m:
        m = re.search(
            rf'<meta[^>]+content=["\']([^"\']+)["\'][^>]+(?:name|property)=["\'](?:og:)?{name}["\']',
            html, re.IGNORECASE,
        )
    return m.group(1).strip() if m else None


def _extract_services(html: str) -> list[str]:
    """Heuristic: find headings inside service/card sections."""
    candidates: list[str] = []

    # Common service section patterns: nav items, h3/h4 inside service divs
    # 1. headings that look like service names (3-6 words, no punctuation)
    headings = _extract_text_block(html, "h3") + _extract_text_block(html, "h4")
    for h in headings:
        h = h.strip()
        word_count = len(h.split())
        if 2 <= word_count <= 7 and not re.search(r"[<>{}@#]", h):
            candidates.append(h)

    # 2. li items inside a ul that seems service-related
    service_section_re = re.compile(
        r'(?:services?|what\s+we\s+do|our\s+work)[^<]*</[^>]+>.*?<ul[^>]*>(.*?)</ul>',
        re.DOTALL | re.IGNORECASE,
    )
    for m in service_section_re.finditer(html):
        items = re.findall(r"<li[^>]*>(.*?)</li>", m.group(1), re.DOTALL | re.IGNORECASE)
        for item in items:
            text = _strip_tags(item).strip()
            if 2 <= len(text.split()) <= 6:
                candidates.append(text)

    # Deduplicate preserving order, cap at 8
    seen: set[str] = set()
    result: list[str] = []
    for c in candidates:
        key = c.lower()
        if key not in seen:
            seen.add(key)
            result.append(c)
        if len(result) >= 8:
            break
    return result


def _extract_testimonials(html: str) -> list[str]:
    """Find blockquote or testimonial/review div text snippets."""
    results: list[str] = []

    # blockquotes
    for text in _extract_text_block(html, "blockquote"):
        text = text.strip()
        if 10 < len(text) < 300:
            results.append(text)

    # divs with testimonial/review class
    for m in re.finditer(
        r'<(?:div|p)[^>]+class=["\'][^"\']*(?:testimonial|review|quote)[^"\']*["\'][^>]*>(.*?)</(?:div|p)>',
        html, re.DOTALL | re.IGNORECASE,
    ):
        text = _strip_tags(m.group(1)).strip()
        if 10 < len(text) < 300:
            results.append(text)

    return results[:4]


def _extract_images(html: str, base_url: str) -> dict[str, list[str]]:
    """Return dict with hero_candidates, logo_candidates, gallery_candidates (absolute URLs)."""
    parsed_base = urlparse(base_url)
    base = f"{parsed_base.scheme}://{parsed_base.netloc}"

    def abs_url(src: str) -> str:
        if src.startswith("//"):
            return f"{parsed_base.scheme}:{src}"
        if src.startswith("http"):
            return src
        return urljoin(base, src)

    all_imgs: list[tuple[str, str, str]] = []  # (src, alt, classes)
    for m in re.finditer(
        r'<img[^>]+src=["\']([^"\']+)["\'][^>]*(?:alt=["\']([^"\']*)["\'])?[^>]*(?:class=["\']([^"\']*)["\'])?',
        html, re.IGNORECASE,
    ):
        src, alt, cls = m.group(1) or "", m.group(2) or "", m.group(3) or ""
        if src and not src.startswith("data:"):
            all_imgs.append((abs_url(src), alt.lower(), cls.lower()))

    # Also check CSS background-image
    for m in re.finditer(r'background-image:\s*url\(["\']?([^"\')\s]+)["\']?\)', html, re.IGNORECASE):
        src = m.group(1)
        if not src.startswith("data:"):
            all_imgs.append((abs_url(src), "", "bg-image"))

    hero_candidates: list[str] = []
    logo_candidates: list[str] = []
    gallery_candidates: list[str] = []

    for src, alt, cls in all_imgs:
        ext = src.split("?")[0].lower()
        if not any(ext.endswith(e) for e in (".jpg", ".jpeg", ".png", ".webp", ".avif")):
            continue

        is_logo = any(k in alt or k in cls or k in src.lower() for k in ("logo", "brand", "icon"))
        is_slider = any(k in src.lower() for k in ("slide", "slider"))
        is_hero = any(k in alt or k in cls or k in src.lower() for k in ("hero", "banner", "header", "bg", "background", "cover"))
        is_gallery = any(k in alt or k in cls or k in src.lower() for k in ("gallery", "portfolio", "work", "photo"))

        if is_logo:
            logo_candidates.append(src)
        elif is_slider:
            # First slider image → hero candidate; subsequent slider images → gallery
            # This prevents slider-based sites from having gallery_count=0
            if not hero_candidates:
                hero_candidates.append(src)
            else:
                gallery_candidates.append(src)
        elif is_hero or "bg-image" in cls:
            hero_candidates.append(src)
        elif is_gallery:
            gallery_candidates.append(src)
        else:
            gallery_candidates.append(src)  # treat unknown images as potential gallery

    return {
        "hero": hero_candidates[:5],
        "logo": logo_candidates[:3],
        "gallery": gallery_candidates[:10],
    }


# ── Image download + quality check ───────────────────────────────────────────

def _is_placeholder_image(image_bytes: bytes) -> bool:
    """Return True if the image is a solid-colour or near-solid-colour placeholder."""
    try:
        img = PILImage.open(BytesIO(image_bytes)).convert("L")  # greyscale
        lo, hi = img.getextrema()
        return (hi - lo) < PLACEHOLDER_COLOUR_RANGE_THRESHOLD
    except Exception:
        return False


def _download_image(url: str, dest_path: Path, client: httpx.Client) -> Optional[ImageAsset]:
    """Download image URL → dest_path. Returns ImageAsset or None on failure."""
    try:
        r = client.get(url, timeout=IMAGE_DOWNLOAD_TIMEOUT_S, follow_redirects=True)
        if r.status_code != 200 or len(r.content) < MIN_IMAGE_BYTES:
            return None
        dest_path.parent.mkdir(parents=True, exist_ok=True)
        dest_path.write_bytes(r.content)
        is_ph = _is_placeholder_image(r.content)
        try:
            img = PILImage.open(BytesIO(r.content))
            w, h = img.size
        except Exception:
            w, h = None, None
        return ImageAsset(
            url=url,
            local_path=str(dest_path),
            width=w,
            height=h,
            bytes=len(r.content),
            is_placeholder=is_ph,
        )
    except Exception as exc:
        log.debug("site_snapshot_image_download_failed", url=url, error=str(exc)[:80])
        return None


def _best_image(candidates: list[str], dest_path: Path, client: httpx.Client) -> Optional[ImageAsset]:
    """Try each candidate URL in order, return first successful non-placeholder download."""
    for url in candidates:
        asset = _download_image(url, dest_path, client)
        if asset and not asset.is_placeholder:
            return asset
    # If all were placeholders, return the first successful one anyway (better than nothing)
    for url in candidates:
        asset = _download_image(url, dest_path, client)
        if asset:
            return asset
    return None


def _whiten_logo_if_dark(logo_path: Path) -> None:
    """If the logo is a dark-coloured RGBA image (dark text/icon on transparent bg),
    replace all opaque pixels with white so it's visible on the dark nav background.
    Operates in-place. No-op if logo is already light or not RGBA."""
    try:
        img = PILImage.open(logo_path).convert("RGBA")
        pixels = list(img.getdata())
        opaque = [(r, g, b, a) for r, g, b, a in pixels if a > 30]
        if not opaque:
            return
        avg_brightness = sum(r + g + b for r, g, b, a in opaque[:200]) // (3 * min(200, len(opaque)))
        if avg_brightness >= 180:
            return  # already light — leave as-is
        # Dark logo: whiten all opaque pixels, preserve alpha
        new_pixels = [
            (255, 255, 255, a) if a > 30 else (r, g, b, a)
            for r, g, b, a in pixels
        ]
        img.putdata(new_pixels)
        # Scale up 2x for retina if small
        w, h = img.size
        if w < 300:
            img = img.resize((w * 2, h * 2), PILImage.LANCZOS)
        # Save back as JPEG on dark background (nav is bg-black/80)
        bg = PILImage.new("RGB", img.size, (15, 15, 15))
        bg.paste(img, (0, 0), img)
        bg.save(logo_path, "JPEG", quality=95)
        log.info("logo_whitened", path=str(logo_path), avg_brightness=avg_brightness)
    except Exception as exc:
        log.debug("logo_whiten_failed", path=str(logo_path), error=str(exc)[:80])


# ── On-demand image fetch (for the mockup_fetch_image tool) ──────────────────
#
# site_snapshot() above only sees a single static HTML fetch of the homepage —
# it never runs JS, so sites with JS-rendered sliders/carousels/galleries
# (e.g. WordPress "revslider" style plugins) often only yield placeholder
# images even though the real photos are visible to anyone with a browser.
# This lets the LLM, after browsing the live site with Playwright MCP (which
# DOES render JS) and finding a real image URL — via browser_evaluate reading
# <img src> attributes, or browser_network_requests — pull that exact URL
# into the local asset pool for use in mockup_copy_assets. Reuses the same
# download/placeholder-detection logic as the static scraper for consistency.

def fetch_extra_image(url: str, slug: str, index: int) -> dict:
    """Download a single image URL (found via Playwright browsing) into the
    slug's asset cache dir. Returns a JSON-serializable dict:
        {"ok": True, "local_path": str, "width": int, "height": int,
         "bytes": int, "is_placeholder": bool}
    or {"ok": False, "error": str} on failure (network error, too small,
    non-200, etc.) — the caller (LLM) should try a different URL rather than
    use a failed result.
    """
    cache_dir = Path(str(mockup_project_dir(slug)).replace(".cache/mockups", ".cache/audits"))
    cache_dir.mkdir(parents=True, exist_ok=True)
    dest_path = cache_dir / f"{slug}-extra-{index}.jpg"
    try:
        with httpx.Client(follow_redirects=True) as client:
            asset = _download_image(url, dest_path, client)
    except Exception as exc:
        return {"ok": False, "error": f"download failed: {exc}"}
    if asset is None:
        return {"ok": False, "error": "download failed (non-200, too small, or network error)"}
    if asset.is_placeholder:
        return {
            "ok": False,
            "error": "image downloaded but looks like a solid-colour/placeholder image, not a real photo",
            "local_path": asset.local_path,
        }
    return {
        "ok": True,
        "local_path": asset.local_path,
        "width": asset.width,
        "height": asset.height,
        "bytes": asset.bytes,
        "is_placeholder": asset.is_placeholder,
    }


# ── Main entry point ─────────────────────────────────────────────────────────

def site_snapshot(website_url: str, slug: str) -> SiteSnapshot:
    """Fetch and analyse the lead's live website. Returns SiteSnapshot.

    Never raises — all errors are captured into snapshot.error.
    Timeout: SNAPSHOT_TIMEOUT_S seconds total.
    """
    started = time.time()
    cache_dir = Path(str(mockup_project_dir(slug)).replace(".cache/mockups", ".cache/audits"))
    cache_dir.mkdir(parents=True, exist_ok=True)

    try:
        with httpx.Client(
            timeout=SNAPSHOT_TIMEOUT_S,
            follow_redirects=True,
            headers={"User-Agent": "Mozilla/5.0 (compatible; ClientCompass/1.0)"},
        ) as client:
            # ── Fetch HTML ────────────────────────────────────────────────────
            try:
                resp = client.get(website_url)
                resp.raise_for_status()
                html = resp.text
                final_url = str(resp.url)
            except Exception as exc:
                return SiteSnapshot(url=website_url, error=f"fetch_failed: {exc!s:.120}")

            # ── Extract text content ──────────────────────────────────────────
            title_m = re.search(r"<title[^>]*>([^<]+)</title>", html, re.IGNORECASE)
            page_title = title_m.group(1).strip() if title_m else None

            h1_m = re.search(r"<h1[^>]*>(.*?)</h1>", html, re.DOTALL | re.IGNORECASE)
            h1 = _strip_tags(h1_m.group(1)).strip() if h1_m else None

            meta_desc = _extract_meta(html, "description")
            services = _extract_services(html)
            testimonials = _extract_testimonials(html)

            phone_m = _PHONE_RE.search(html)
            phone = phone_m.group(0).strip() if phone_m else None

            email_m = _EMAIL_RE.search(html)
            email = email_m.group(0).strip() if email_m else None

            rating_m = _RATING_RE.search(html)
            google_rating = float(rating_m.group(1) or rating_m.group(2)) if rating_m else None

            review_m = _REVIEW_COUNT_RE.search(html)
            review_count = int(review_m.group(1)) if review_m else None

            # ── Extract image URLs ────────────────────────────────────────────
            image_candidates = _extract_images(html, final_url)

            # ── Download images ───────────────────────────────────────────────
            hero_asset = _best_image(
                image_candidates["hero"],
                cache_dir / f"{slug}-scraped-hero.jpg",
                client,
            )
            logo_asset = _best_image(
                image_candidates["logo"],
                cache_dir / f"{slug}-scraped-logo.jpg",
                client,
            )
            if logo_asset:
                _whiten_logo_if_dark(Path(logo_asset.local_path))

            gallery_assets: list[ImageAsset] = []
            for i, url in enumerate(image_candidates["gallery"], 1):
                if len(gallery_assets) >= MAX_GALLERY_IMAGES:
                    break
                dest = cache_dir / f"{slug}-scraped-g{i}.jpg"
                asset = _download_image(url, dest, client)
                if asset:
                    gallery_assets.append(asset)

            elapsed = int((time.time() - started) * 1000)
            log.info(
                "site_snapshot_complete",
                url=website_url,
                slug=slug,
                elapsed_ms=elapsed,
                services_found=len(services),
                hero_ok=hero_asset is not None,
                logo_ok=logo_asset is not None,
                gallery_count=len(gallery_assets),
            )

            return SiteSnapshot(
                url=final_url,
                page_title=page_title,
                h1=h1,
                meta_description=meta_desc,
                services=services,
                phone=phone,
                email=email,
                testimonials=testimonials,
                google_rating=google_rating,
                review_count=review_count,
                hero=hero_asset,
                logo=logo_asset,
                gallery=gallery_assets,
            )

    except Exception as exc:
        log.warning("site_snapshot_failed", url=website_url, error=str(exc)[:200])
        return SiteSnapshot(url=website_url, error=str(exc)[:200])


def format_snapshot_block(snapshot: Optional[SiteSnapshot]) -> str:
    """Format a SiteSnapshot as an XML block for injection into the Pi prompt."""
    if snapshot is None:
        return "<site_snapshot>NOT_AVAILABLE: no website URL on record.</site_snapshot>"

    if snapshot.error:
        return f"<site_snapshot>ERROR: {snapshot.error}\nFall back to DB data only.</site_snapshot>"

    lines = ["<site_snapshot>"]
    if snapshot.page_title:
        lines.append(f"  <page_title>{snapshot.page_title}</page_title>")
    if snapshot.h1:
        lines.append(f"  <h1>{snapshot.h1}</h1>")
    if snapshot.meta_description:
        lines.append(f"  <meta_description>{snapshot.meta_description}</meta_description>")
    if snapshot.phone:
        lines.append(f"  <phone>{snapshot.phone}</phone>")
    if snapshot.email:
        lines.append(f"  <email>{snapshot.email}</email>")
    if snapshot.google_rating is not None:
        lines.append(f"  <google_rating>{snapshot.google_rating} ({snapshot.review_count or '?'} reviews)</google_rating>")
    if snapshot.services:
        lines.append("  <services>")
        for s in snapshot.services:
            lines.append(f"    <service>{s}</service>")
        lines.append("  </services>")
    if snapshot.testimonials:
        lines.append("  <testimonials>")
        for t in snapshot.testimonials:
            # Escape any XML special chars in testimonial text
            t_safe = t.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
            lines.append(f"    <testimonial>{t_safe}</testimonial>")
        lines.append("  </testimonials>")

    lines.append("  <images>")
    if snapshot.hero:
        lines.append(f"    {snapshot.hero.to_xml('hero')}")
    else:
        lines.append("    <hero available=\"false\"/>")
    if snapshot.logo:
        lines.append(f"    {snapshot.logo.to_xml('logo')}")
    else:
        lines.append("    <logo available=\"false\"/>")
    if snapshot.gallery:
        lines.append(f"    <gallery count=\"{len(snapshot.gallery)}\">")
        for i, g in enumerate(snapshot.gallery, 1):
            lines.append(f"      {g.to_xml(f'img_{i}')}")
        lines.append("    </gallery>")
    lines.append("  </images>")

    lines.append(f"  <scraped_at>{snapshot.scraped_at}</scraped_at>")
    lines.append("</site_snapshot>")
    return "\n".join(lines)
