"""App settings store — small key/value read/write helper backed by `app_settings`.

The admin portal writes to this table directly over the existing
cross-Tailscale `leadgenQuery` path (same pattern as mockup approvals / lead
rejection). Workers read it here, no caching — these are checked at most a
few times per task run (daily/hourly), so a fresh read each time is simpler
and avoids stale-toggle surprises (see rejected_websites.py for a cached
version of this same trade-off, used at much higher call frequency).
"""
from __future__ import annotations

from app.db.sync_session import sync_session_scope
from app.models import AppSetting
from app.utils.logger import get_logger

log = get_logger(__name__)


def get_setting(key: str, default: str | None = None) -> str | None:
    """Return the raw string value for `key`, or `default` if unset/unreachable."""
    try:
        with sync_session_scope() as session:
            row = session.get(AppSetting, key)
            return row.value if row else default
    except Exception as exc:
        log.warning("app_setting_read_failed", key=key, error=str(exc))
        return default


def get_bool_setting(key: str, default: bool = True) -> bool:
    """Return `key` parsed as a boolean ('true'/'1' → True, anything else → False)."""
    raw = get_setting(key, default=None)
    if raw is None:
        return default
    return raw.strip().lower() in ("true", "1", "yes", "on")


def set_setting(key: str, value: str) -> None:
    """Upsert `key` = `value`. Used by scripts/manual toggles; the admin portal
    writes directly via SQL over its own leadgen DB connection."""
    with sync_session_scope() as session:
        row = session.get(AppSetting, key)
        if row:
            row.value = value
        else:
            session.add(AppSetting(key=key, value=value))
