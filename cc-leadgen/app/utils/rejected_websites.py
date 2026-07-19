"""Rejected websites store — loads from DB + CSV config, normalises URLs for matching."""
from __future__ import annotations

from datetime import datetime, timezone

from typing import Optional
from urllib.parse import urlparse

from app.config import get_settings
from app.db.sync_session import sync_session_scope
from app.models import RejectedWebsite
from app.utils.logger import get_logger

log = get_logger(__name__)

# Module-level cache so we don't hit the DB on every discovery batch.
# Cleared on every call to refresh() so operators can add entries and see them
# picked up within the next discovery run.
_cache: set[str] = set()


def _normalise(website: str) -> str:
    """Strip scheme, www prefix, and trailing slash so we match on domain root."""
    website = website.strip().rstrip("/")
    if website.lower().startswith(("http://", "https://")):
        parsed = urlparse(website)
        host = parsed.netloc or parsed.path
    else:
        host = website
    # Strip leading www.
    host = host.lower().lstrip("www.").rstrip("/")
    return host


def refresh() -> None:
    """Reload the rejected-websites cache from DB + CSV config."""
    global _cache
    rejected: set[str] = set()

    # 1. DB entries
    try:
        with sync_session_scope() as session:
            rows = session.query(RejectedWebsite.website).all()
            for (ws,) in rows:
                if ws:
                    rejected.add(_normalise(ws))
    except Exception as exc:
        log.warning("rejected_websites_db_load_failed", error=str(exc))

    # 2. CSV config fallback (for quick operator edits without DB)
    settings = get_settings()
    csv_raw = getattr(settings, "rejected_websites_csv", "") or ""
    for entry in csv_raw.split(","):
        entry = entry.strip()
        if entry:
            rejected.add(_normalise(entry))

    _cache = rejected
    log.info("rejected_websites_cache_refreshed", count=len(_cache))


def is_rejected(website: str | None) -> bool:
    """Return True if the website's normalised domain matches a rejected entry.

    website may be a full URL or just a domain. Leading/trailing whitespace
    and the scheme/www prefix are stripped before matching.
    """
    if not website:
        return False
    norm = _normalise(website)
    if not norm:
        return False
    # Refresh every call so newly added entries take effect immediately
    # (fast for most calls since cache is module-level, but also handles
    # the first call of a fresh worker process).
    if not _cache:
        refresh()
    return norm in _cache


def add(website: str, reason: str | None = None) -> bool:
    """Insert a rejected website into the DB if it does not already exist.

    Returns True if the entry was added, False if it already existed.
    """
    norm = _normalise(website)
    if not norm:
        return False
    try:
        with sync_session_scope() as session:
            existing = session.query(RejectedWebsite).filter(
                RejectedWebsite.website == website
            ).first()
            if existing:
                return False
            session.add(RejectedWebsite(website=website, reason=reason, rejected_at=datetime.now(timezone.utc)))
        refresh()  # update cache after insert
        return True
    except Exception as exc:
        log.error("rejected_website_add_failed", website=website, error=str(exc))
        return False


def remove(website: str) -> bool:
    """Remove a rejected website from the DB. Returns True if deleted."""
    try:
        with sync_session_scope() as session:
            rows = session.query(RejectedWebsite).filter(
                RejectedWebsite.website == website
            ).delete()
        refresh()
        return rows > 0
    except Exception as exc:
        log.error("rejected_website_remove_failed", website=website, error=str(exc))
        return False


def list_all() -> list[tuple[str, Optional[str]]]:
    """Return all rejected websites as (website, reason) tuples."""
    try:
        with sync_session_scope() as session:
            rows = session.query(RejectedWebsite.website, RejectedWebsite.reason).all()
            return [(ws, reason) for ws, reason in rows]
    except Exception as exc:
        log.error("rejected_website_list_failed", error=str(exc))
        return []
