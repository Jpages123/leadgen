"""Pexels candidate search for the social pipeline — metadata only.

Unlike app.workers.mockup_helpers.stock_images (which downloads images for
the mockup builder), this module only collects candidate metadata
[{id, photographer, pexels_url, alt, thumb, original}] for operator review in
the admin portal. The chosen photo is fetched and stored by login-portal.
"""
from __future__ import annotations

import httpx

from app.config import get_settings
from app.utils.logger import get_logger

log = get_logger(__name__)

PEXELS_SEARCH_URL = "https://api.pexels.com/v1/search"
SEARCH_TIMEOUT_S = 8


def search_photos(
    query: str,
    *,
    orientation: str = "portrait",
    per_page: int = 9,
    settings=None,
) -> list[dict]:
    """One Pexels search → [{id, photographer, pexels_url, alt, thumb, original}].

    Raises on missing API key / transport / HTTP errors — callers decide
    whether failure is tolerated (ingest) or surfaced (operator re-search).
    """
    settings = settings or get_settings()
    if not settings.pexels_api_key:
        raise RuntimeError("PEXELS_API_KEY not configured")
    resp = httpx.get(
        PEXELS_SEARCH_URL,
        params={"query": query, "orientation": orientation, "per_page": per_page},
        headers={"Authorization": settings.pexels_api_key},
        timeout=SEARCH_TIMEOUT_S,
    )
    resp.raise_for_status()
    out: list[dict] = []
    for p in resp.json().get("photos", []):
        src = p.get("src") or {}
        out.append({
            "id": p.get("id"),
            "photographer": p.get("photographer"),
            "pexels_url": p.get("url"),
            "alt": p.get("alt"),
            "thumb": src.get("medium"),
            "original": src.get("original"),
        })
    return out


def gather_candidates(
    queries: list[str],
    *,
    max_queries: int = 3,
    per_page: int = 4,
    cap: int = 9,
    settings=None,
) -> list[dict]:
    """Search up to ``max_queries`` portrait queries, dedupe by Pexels id, cap.

    Per-query failures are logged and skipped so ingest still succeeds —
    worst case the row lands with empty candidates and the operator uses
    the search box in the portal.
    """
    seen: set = set()
    out: list[dict] = []
    for q in list(queries or [])[:max_queries]:
        try:
            results = search_photos(q, orientation="portrait", per_page=per_page, settings=settings)
        except Exception as exc:
            log.warning("social_pexels_query_failed", query=q, error=str(exc)[:200])
            continue
        for cand in results:
            cid = cand.get("id")
            if cid is None or cid in seen:
                continue
            seen.add(cid)
            out.append(cand)
            if len(out) >= cap:
                return out
    return out
