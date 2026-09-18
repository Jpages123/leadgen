"""Celery task: sync mockup approval decisions from prod DB back to leadgen DB.

The admin portal (on prod server) writes approve/reject to admin_crm.mockup_approvals.
This task polls that table and updates leads.mockup_status in the local leadgen DB,
so the outreach worker can pick up newly approved leads on its next run.

When a new approval is detected, also triggers generate_email_draft so the operator
can review and send the email from /admin/email-drafts instead of auto-send.

Runs every 5 minutes via Celery Beat.
"""
from __future__ import annotations

from urllib.parse import urlparse, unquote

from celery import shared_task

from app.config import get_settings
from app.db.sync_session import sync_session_scope
from app.models import Lead
from app.utils.logger import get_logger

log = get_logger(__name__)


def _parse_prod_db_url(raw: str) -> dict:
    """Parse PROD_DB_URL safely (handles @ in password)."""
    cleaned = raw.replace("+asyncpg", "")
    parsed = urlparse(cleaned)
    return {
        "user": unquote(parsed.username or ""),
        "password": unquote(parsed.password or ""),
        "host": parsed.hostname,
        "port": parsed.port or 5432,
        "database": (parsed.path or "/").lstrip("/"),
    }


def _get_prod_conn():
    """Return a psycopg2 connection to the prod DB."""
    import psycopg2
    settings = get_settings()
    prod_url = settings.prod_db_url
    if not prod_url:
        raise RuntimeError("PROD_DB_URL not configured")
    cfg = _parse_prod_db_url(prod_url)
    return psycopg2.connect(
        user=cfg["user"], password=cfg["password"],
        host=cfg["host"], port=cfg["port"], dbname=cfg["database"],
        connect_timeout=10,
    )


@shared_task(bind=True, name="app.workers.mockup_approval_sync.tasks.sync_approvals")
def sync_approvals(self) -> dict:
    """Poll prod DB for actioned mockup approvals and sync status to leadgen DB.

    Returns:
        dict with keys: status, synced, drafts_triggered
    """
    try:
        conn = _get_prod_conn()
    except Exception as exc:
        log.warning("mockup_sync_prod_db_unavailable", error=str(exc))
        return {"status": "skipped", "reason": str(exc)}

    synced = 0
    newly_approved_ids = []
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute("SET LOCAL app.is_admin = 'true'")
                # Pull approval_id too so we can trigger draft generation
                cur.execute(
                    """
                    SELECT id, lead_id, status
                    FROM admin_crm.mockup_approvals
                    WHERE status IN ('approved', 'rejected')
                      AND actioned_at IS NOT NULL
                    """
                )
                rows = cur.fetchall()
    finally:
        conn.close()

    if not rows:
        return {"status": "ok", "synced": 0, "drafts_triggered": 0}

    with sync_session_scope() as session:
        for approval_id, lead_id_str, new_status in rows:
            lead = session.get(Lead, lead_id_str)
            if lead is None:
                continue
            if lead.mockup_status == new_status:
                # Already synced this status — but if approved, ensure draft exists
                if new_status == "approved":
                    newly_approved_ids.append(approval_id)
                continue
            lead.mockup_status = new_status
            session.add(lead)
            synced += 1
            if new_status == "approved":
                newly_approved_ids.append(approval_id)
            log.info("mockup_status_synced", lead_id=lead_id_str, status=new_status)

    # Trigger draft generation for newly-approved mockups
    drafts_triggered = 0
    if newly_approved_ids:
        # Import here to avoid circular imports at module load
        from app.workers.email_draft import generate_email_draft
        for approval_id in newly_approved_ids:
            try:
                generate_email_draft.delay(str(approval_id))
                drafts_triggered += 1
            except Exception as exc:
                log.warning(
                    "draft_dispatch_failed",
                    approval_id=str(approval_id),
                    error=str(exc),
                )

    log.info(
        "mockup_sync_done",
        synced=synced,
        drafts_triggered=drafts_triggered,
    )
    return {"status": "ok", "synced": synced, "drafts_triggered": drafts_triggered}