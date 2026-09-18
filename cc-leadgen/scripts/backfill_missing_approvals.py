"""Backfill missing admin_crm.mockup_approvals rows.

Context (2026-08-03): VPS Postgres port 5432 wasn't bound to the Tailscale
interface (regression from the e47b523 Docker secrets refactor), so the
laptop-side cc-leadgen worker couldn't reach the prod admin DB. Every
mockup_write_approval call silently failed for ~13 days, meaning successful
mockups have a mockup_status='pending_approval' row in the leadgen DB but
NO corresponding row in admin_crm.mockup_approvals — so the operator can't
approve them via /admin/mockup-approvals.

This script:
  1. Reads all pending_approval leads with URLs from the leadgen DB
  2. Filters out the ones already present in admin_crm.mockup_approvals
  3. Inserts the missing rows with status='pending' (so they show up in
     the admin UI), using the same column shape as write_approval.write_approval()

Idempotent: re-running after the port is restored will only insert rows that
are still missing.

Usage:
  source .venv/bin/activate
  python scripts/backfill_missing_approvals.py
  python scripts/backfill_missing_approvals.py --dry-run
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from urllib.parse import urlparse, unquote

import psycopg2

# Reuse the same logic as the runtime helper, so the column shape stays in sync.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from app.config import get_settings  # noqa: E402
from app.db.sync_session import sync_session_scope  # noqa: E402
from app.models.lead import Lead  # noqa: E402


def _parse_prod_db_url(raw: str) -> dict:
    cleaned = raw.replace("+asyncpg", "")
    parsed = urlparse(cleaned)
    return {
        "user": unquote(parsed.username or ""),
        "password": unquote(parsed.password or ""),
        "host": parsed.hostname,
        "port": parsed.port or 5432,
        "database": (parsed.path or "/").lstrip("/"),
    }


def _existing_lead_ids(cur) -> set[str]:
    cur.execute("SELECT lead_id FROM admin_crm.mockup_approvals")
    return {str(r[0]) for r in cur.fetchall()}


def _candidate_leads() -> list[Lead]:
    """All leadgen leads currently in pending_approval with a mockup URL."""
    with sync_session_scope() as session:
        leads = (
            session.query(Lead)
            .filter(Lead.mockup_status == "pending_approval")
            .filter(Lead.mockup_url.isnot(None))
            .filter(Lead.mockup_url != "")
            .order_by(Lead.mockup_generated_at.desc())
            .all()
        )
    return leads


def _recommendation_for(lead: Lead) -> dict:
    """Build a minimal recommendation payload matching the runtime shape.

    The runtime helper pulls recommendation from the Pi skill's output. For
    backfill we don't have the original summary — we just store a stub that
    records this row was backfilled, with enough metadata for the admin UI.
    """
    return {
        "status": "backfilled",
        "rationale": (
            "Approval row backfilled on 2026-08-03 after the VPS Postgres "
            "port 5432 was restored. Original Pi summary was not captured — "
            "mockup itself is live on Cloudflare Pages."
        ),
        "mockup_url": lead.mockup_url,
        "iterations": None,
        "template": None,
        "backfilled_at": "2026-08-03T10:25:00Z",
    }


def backfill(dry_run: bool = False) -> dict:
    settings = get_settings()
    prod_url = settings.prod_db_url
    if not prod_url:
        raise RuntimeError("PROD_DB_URL not configured in app.config")

    cfg = _parse_prod_db_url(prod_url)
    candidates = _candidate_leads()
    print(f"Found {len(candidates)} pending_approval leads with URLs in leadgen DB")

    conn = psycopg2.connect(
        user=cfg["user"], password=cfg["password"],
        host=cfg["host"], port=cfg["port"], dbname=cfg["database"],
    )
    inserted = 0
    skipped_existing = 0
    errors: list[str] = []
    try:
        with conn.cursor() as cur:
            existing = _existing_lead_ids(cur)
            print(f"Found {len(existing)} existing rows in admin_crm.mockup_approvals")

            for lead in candidates:
                lead_id = str(lead.id)
                if lead_id in existing:
                    skipped_existing += 1
                    continue

                if dry_run:
                    print(f"  [DRY] would insert: {lead.business_name!r} "
                          f"({lead_id}) → {lead.mockup_url}")
                    inserted += 1
                    continue

                try:
                    cur.execute(
                        """
                        INSERT INTO admin_crm.mockup_approvals
                            (lead_id, business_name, website, mockup_url, vertical,
                             web_pitch_score, pagespeed_mobile, website_platform,
                             city, email, status, created_at, updated_at,
                             lead_type, recommendation)
                        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                                'pending', NOW(), NOW(), 'bad_website', %s::jsonb)
                        ON CONFLICT (lead_id) DO NOTHING
                        """,
                        (
                            lead_id,
                            lead.business_name,
                            lead.website,
                            lead.mockup_url,
                            lead.business_type,
                            lead.web_pitch_score,
                            lead.pagespeed_mobile,
                            lead.website_platform,
                            lead.city,
                            lead.email,
                            json.dumps(_recommendation_for(lead)),
                        ),
                    )
                    inserted += 1
                    print(f"  ✓ inserted: {lead.business_name!r} ({lead_id})")
                except Exception as exc:
                    errors.append(f"{lead_id} ({lead.business_name}): {exc}")

            if not dry_run:
                conn.commit()
    finally:
        conn.close()

    return {
        "candidates": len(candidates),
        "skipped_existing": skipped_existing,
        "inserted": inserted,
        "errors": errors,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--dry-run", action="store_true",
                        help="Show what would be inserted without writing")
    args = parser.parse_args()

    result = backfill(dry_run=args.dry_run)

    if args.dry_run:
        print(f"\nDRY RUN: would insert {result.get('inserted', 0)} rows "
              f"(skipped {result['skipped_existing']} already-existing)")
    else:
        print(f"\nInserted: {result['inserted']}")
        print(f"Skipped (already in prod DB): {result['skipped_existing']}")
        print(f"Errors: {len(result['errors'])}")
        for err in result["errors"]:
            print(f"  - {err}")

    return 0 if not result["errors"] else 1


if __name__ == "__main__":
    raise SystemExit(main())