"""stock_images.py — Pexels stock-photo fallback for mockup image slots.

Phase P (docs/PHASE_P_PLAN.md in the cc-web-revamp docs repo). Used when the
lead's own website (site_snapshot.py static scrape + Playwright browsing via
mockup_fetch_image) doesn't have enough real, on-topic photos for a slot —
previously this fell straight to a flat Pillow gradient/solid-colour
placeholder, requiring an operator to manually source and paste stock image
URLs after the fact.

This module only *fetches candidates* — it never picks a winner itself.
Downloaded candidates are handed back to the Pi agent (via the
mockup_search_stock_images tool in mockup-builder.ts) as images in its own
context, so its vision (MiniMax M3 is natively multimodal — same mechanism
already used for the mockup_screenshot review gate) picks the best fit. The
winning local_path then flows into mockup_copy_assets as an ordinary slot
hint, same as an LLM-nominated scraped image.

Pexels: no attribution required, free for commercial use, no watermarks —
https://www.pexels.com/license/
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path
from typing import Optional

import httpx
from PIL import Image as PILImage

from app.config import get_settings
from app.utils.logger import get_logger
from app.utils.mockup_build import mockup_project_dir

log = get_logger(__name__)

PEXELS_SEARCH_URL = "https://api.pexels.com/v1/search"
SEARCH_TIMEOUT_S = 8
IMAGE_DOWNLOAD_TIMEOUT_S = 8
MIN_IMAGE_BYTES = 2048  # anything smaller is almost certainly a broken/tiny image
PLACEHOLDER_COLOUR_RANGE_THRESHOLD = 40  # px range in greyscale — below = solid colour
DEFAULT_PER_PAGE = 5
MAX_PER_PAGE = 10
ORIENTATIONS = ("landscape", "portrait", "square")


@dataclass
class StockCandidate:
    url: str                      # Pexels source image URL that was downloaded
    local_path: str
    width: Optional[int] = None
    height: Optional[int] = None
    bytes: int = 0
    is_placeholder: bool = False
    photographer: Optional[str] = None
    pexels_url: Optional[str] = None
    alt: Optional[str] = None


def _cache_dir(slug: str) -> Path:
    cache_dir = Path(str(mockup_project_dir(slug)).replace(".cache/mockups", ".cache/audits"))
    cache_dir.mkdir(parents=True, exist_ok=True)
    return cache_dir


def _query_hash(query: str) -> str:
    return hashlib.sha1(query.encode("utf-8")).hexdigest()[:8]


def _is_placeholder_image(image_bytes: bytes) -> bool:
    try:
        img = PILImage.open(BytesIO(image_bytes)).convert("L")
        lo, hi = img.getextrema()
        return (hi - lo) < PLACEHOLDER_COLOUR_RANGE_THRESHOLD
    except Exception:
        return False


def _record_credits(cache_dir: Path, slug: str, credits: list[dict]) -> None:
    """Append photographer/licence provenance for every downloaded stock image.

    Pexels doesn't require attribution, but this is cheap insurance and also
    lets the admin UI show "sourced from stock" vs "scraped from lead site"
    (image_audit's <slot>_source field handles the coarse version of this;
    this file is the detailed record).
    """
    if not credits:
        return
    credits_path = cache_dir / f"{slug}-stock-credits.json"
    existing: list[dict] = []
    if credits_path.exists():
        try:
            existing = json.loads(credits_path.read_text())
        except Exception:
            existing = []
    existing.extend(credits)
    credits_path.write_text(json.dumps(existing, indent=2))


def search_stock_images(
    query: str,
    slug: str,
    *,
    orientation: str = "landscape",
    per_page: int = DEFAULT_PER_PAGE,
) -> list[StockCandidate]:
    """Search Pexels and download the top ``per_page`` results.

    Never raises — network/API/config errors return an empty list so the
    caller can report "no results" and try a different query or fall
    through to the next fallback tier (Pillow gradient), exactly like
    ``site_snapshot.fetch_extra_image`` does on download failure.
    """
    settings = get_settings()
    api_key = settings.pexels_api_key
    if not api_key:
        log.warning("stock_images_no_api_key", slug=slug)
        return []

    orientation = orientation if orientation in ORIENTATIONS else "landscape"
    per_page = max(1, min(per_page, MAX_PER_PAGE))

    try:
        with httpx.Client(timeout=SEARCH_TIMEOUT_S) as client:
            resp = client.get(
                PEXELS_SEARCH_URL,
                params={"query": query, "per_page": per_page, "orientation": orientation},
                headers={"Authorization": api_key},
            )
            resp.raise_for_status()
            data = resp.json()
    except Exception as exc:
        log.warning("stock_images_search_failed", slug=slug, query=query, error=str(exc)[:200])
        return []

    photos = data.get("photos", [])
    if not photos:
        log.info("stock_images_no_results", slug=slug, query=query, orientation=orientation)
        return []

    cache_dir = _cache_dir(slug)
    qhash = _query_hash(query)
    candidates: list[StockCandidate] = []
    credits: list[dict] = []

    with httpx.Client(timeout=IMAGE_DOWNLOAD_TIMEOUT_S, follow_redirects=True) as client:
        for i, p in enumerate(photos):
            src_urls = p.get("src") or {}
            src = src_urls.get("large") or src_urls.get("original") or src_urls.get("large2x")
            if not src:
                continue
            dest = cache_dir / f"{slug}-stock-{qhash}-{i}.jpg"
            try:
                r = client.get(src)
                if r.status_code != 200 or len(r.content) < MIN_IMAGE_BYTES:
                    continue
                is_ph = _is_placeholder_image(r.content)
                img = PILImage.open(BytesIO(r.content)).convert("RGB")
                img.save(dest, "JPEG", quality=88)
                w, h = img.size
            except Exception as exc:
                log.debug("stock_images_download_failed", url=src, error=str(exc)[:80])
                continue

            cand = StockCandidate(
                url=src,
                local_path=str(dest),
                width=w,
                height=h,
                bytes=dest.stat().st_size,
                is_placeholder=is_ph,
                photographer=p.get("photographer"),
                pexels_url=p.get("url"),
                alt=p.get("alt"),
            )
            candidates.append(cand)
            credits.append({
                "local_path": cand.local_path,
                "photographer": cand.photographer,
                "pexels_url": cand.pexels_url,
                "query": query,
            })

    _record_credits(cache_dir, slug, credits)

    log.info(
        "stock_images_search_complete",
        slug=slug, query=query, orientation=orientation,
        results=len(photos), downloaded=len(candidates),
    )
    return candidates


def search_stock_images_json(
    query: str,
    slug: str,
    orientation: str = "landscape",
    per_page: int = DEFAULT_PER_PAGE,
) -> dict:
    """JSON-serializable wrapper for the CLI/Pi tool."""
    candidates = search_stock_images(query, slug, orientation=orientation, per_page=per_page)
    if not candidates:
        return {
            "ok": False,
            "error": "no usable results (empty Pexels search, missing API key, or all candidates too small/failed to download)",
            "query": query,
        }
    return {
        "ok": True,
        "query": query,
        "orientation": orientation,
        "count": len(candidates),
        "candidates": [
            {
                "local_path": c.local_path,
                "width": c.width,
                "height": c.height,
                "bytes": c.bytes,
                "is_placeholder": c.is_placeholder,
                "photographer": c.photographer,
                "pexels_url": c.pexels_url,
                "alt": c.alt,
            }
            for c in candidates
        ],
    }
