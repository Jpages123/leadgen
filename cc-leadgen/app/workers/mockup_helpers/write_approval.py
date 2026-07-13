"""Write approval row to prod admin DB (admin_crm.mockup_approvals)."""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from urllib.parse import urlparse, unquote

import psycopg2


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


def write_approval(lead_id: str, mockup_url: str, recommendation: dict) -> dict:
    """Insert a pending approval row in admin_crm.mockup_approvals.

    Returns {approval_id, lead_id, status}.
    """
    prod_url = os.environ.get("PROD_DB_URL") or _get_settings_prod_url()
    if not prod_url:
        raise RuntimeError("PROD_DB_URL not set in env or app.config")

    cfg = _parse_prod_db_url(prod_url)

    # Pull leadgen data via the leadgen DB connection too, so we have
    # business_name, vertical, web_pitch_score, etc. for the admin row.
    from app.db.sync_session import sync_session_scope
    from app.models.lead import Lead
    from sqlalchemy import select

    with sync_session_scope() as session:
        lead = session.get(Lead, lead_id)
        if not lead:
            raise ValueError(f"lead {lead_id} not found in leadgen DB")

        business_name = lead.business_name
        website = lead.website
        vertical = lead.business_type
        web_pitch_score = lead.web_pitch_score
        pagespeed_mobile = lead.pagespeed_mobile
        website_platform = lead.website_platform
        city = lead.city
        email = lead.email

    conn = psycopg2.connect(
        user=cfg["user"], password=cfg["password"],
        host=cfg["host"], port=cfg["port"], dbname=cfg["database"],
    )
    conn.autocommit = False
    try:
        with conn.cursor() as cur:
            # Upsert: if a row exists for this lead_id, update it to pending
            cur.execute("""
                INSERT INTO admin_crm.mockup_approvals
                    (lead_id, business_name, website, mockup_url, vertical,
                     web_pitch_score, pagespeed_mobile, website_platform, city,
                     email, status, created_at, updated_at, recommendation)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 'pending', NOW(), NOW(), %s::jsonb)
                ON CONFLICT (lead_id) DO UPDATE SET
                    mockup_url = EXCLUDED.mockup_url,
                    status = 'pending',
                    actioned_at = NULL,
                    reject_reason = NULL,
                    updated_at = NOW(),
                    recommendation = EXCLUDED.recommendation
                RETURNING id
            """, (
                lead_id, business_name, website, mockup_url, vertical,
                web_pitch_score, pagespeed_mobile, website_platform, city,
                email, json.dumps(recommendation),
            ))
            row = cur.fetchone()
            approval_id = str(row[0]) if row else None
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()

    return {
        "approval_id": approval_id,
        "lead_id": lead_id,
        "status": "pending",
        "mockup_url": mockup_url,
    }


def _get_settings_prod_url() -> str:
    from app.config import get_settings
    return get_settings().prod_db_url
