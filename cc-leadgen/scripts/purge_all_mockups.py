#!/usr/bin/env python3
"""
Purge ALL generated mockups (per 2026-07-13 operator directive).
- Leadgen DB: clear mockup_status, mockup_url, mockup_generated_at for all leads
- Cloudflare: delete all <slug>-demo Pages projects + demo-<slug> DNS A records
- Prod admin DB: delete all rows in admin_crm.mockup_approvals

Run on the home laptop, inside the worker container so we use the same
Cloudflare API token + DB credentials.

Usage:
    docker compose exec -T worker .venv/bin/python -m scripts.purge_all_mockups --dry-run
    docker compose exec -T worker .venv/bin/python -m scripts.purge_all_mockups
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from urllib.parse import urlparse, unquote

sys.path.insert(0, "/app")

from app.config import get_settings  # noqa: E402
from app.db.sync_session import sync_session_scope  # noqa: E402
from app.models.lead import Lead  # noqa: E402
from app.utils.cloudflare_deploy import (  # noqa: E402
    delete_pages_project,
    delete_dns_record,
    list_pages_projects,
    list_demo_dns_records,
)
from sqlalchemy import select  # noqa: E402


def _parse_prod_db_url(raw: str) -> dict:
    """Parse PROD_DB_URL safely (handles @ in password).

    Mirrors app/workers/mockup_generator.py line ~155 pattern.
    """
    cleaned = raw.replace("+asyncpg", "")
    parsed = urlparse(cleaned)
    return {
        "user": unquote(parsed.username or ""),
        "password": unquote(parsed.password or ""),
        "host": parsed.hostname,
        "port": parsed.port or 5432,
        "database": (parsed.path or "/").lstrip("/"),
    }


def purge_leadgen_db(dry_run: bool) -> dict:
    """Reset all leads' mockup fields to 'none' (mockup_status is NOT NULL)."""
    counts = {"total_with_status": 0, "total_with_url": 0, "purged": 0}
    with sync_session_scope() as session:
        rows = session.execute(
            select(Lead).where(
                (Lead.mockup_status.isnot(None)) | (Lead.mockup_url.isnot(None))
            )
        ).scalars().all()
        counts["total_with_status"] = sum(1 for r in rows if r.mockup_status is not None and r.mockup_status != "none")
        counts["total_with_url"] = sum(1 for r in rows if r.mockup_url is not None)

        if dry_run:
            return counts

        for lead in rows:
            lead.mockup_status = "none"
            lead.mockup_url = None
            lead.mockup_generated_at = None
            session.add(lead)
            counts["purged"] += 1

    return counts


def purge_cloudflare(dry_run: bool) -> dict:
    """Delete all <slug>-demo Pages projects and demo-<slug> DNS A records."""
    counts = {"pages_projects_found": 0, "pages_projects_deleted": 0,
              "dns_records_found": 0, "dns_records_deleted": 0,
              "errors": []}

    # Pages projects
    try:
        projects = list_pages_projects()
        demo_projects = [p for p in projects if p["name"].endswith("-demo")]
        counts["pages_projects_found"] = len(demo_projects)
        if not dry_run:
            for p in demo_projects:
                try:
                    delete_pages_project(p["name"])
                    counts["pages_projects_deleted"] += 1
                except Exception as e:
                    counts["errors"].append(f"pages/{p['name']}: {str(e)[:100]}")
    except Exception as e:
        counts["errors"].append(f"list_pages_projects: {str(e)[:200]}")

    # DNS records
    try:
        records = list_demo_dns_records()
        counts["dns_records_found"] = len(records)
        if not dry_run:
            for r in records:
                try:
                    delete_dns_record(r["id"])
                    counts["dns_records_deleted"] += 1
                except Exception as e:
                    counts["errors"].append(f"dns/{r.get('name')}: {str(e)[:100]}")
    except Exception as e:
        counts["errors"].append(f"list_demo_dns_records: {str(e)[:200]}")

    return counts


def purge_prod_admin_db(dry_run: bool) -> dict:
    """Delete all rows in admin_crm.mockup_approvals on prod DB."""
    import psycopg2
    prod_url = os.environ.get("PROD_DB_URL")
    if not prod_url:
        return {"skipped": True, "reason": "PROD_DB_URL not set"}

    cfg = _parse_prod_db_url(prod_url)
    counts = {"total": 0, "deleted": 0}
    try:
        conn = psycopg2.connect(
            user=cfg["user"], password=cfg["password"],
            host=cfg["host"], port=cfg["port"], dbname=cfg["database"],
        )
        conn.autocommit = False
        cur = conn.cursor()
        cur.execute("SELECT COUNT(*) FROM admin_crm.mockup_approvals")
        counts["total"] = cur.fetchone()[0]

        if dry_run:
            conn.rollback()
            conn.close()
            return counts

        cur.execute("DELETE FROM admin_crm.mockup_approvals")
        counts["deleted"] = cur.rowcount
        conn.commit()
        conn.close()
    except Exception as e:
        counts["error"] = str(e)

    return counts


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true",
                        help="Show what would be deleted without actually deleting")
    args = parser.parse_args()

    print("=" * 60)
    print("PURGE ALL MOCKUPS" + (" (DRY RUN)" if args.dry_run else ""))
    print("=" * 60)

    print("\n[1/3] Leadgen DB...")
    r1 = purge_leadgen_db(dry_run=args.dry_run)
    print(json.dumps(r1, indent=2))

    print("\n[2/3] Cloudflare Pages + DNS...")
    r2 = purge_cloudflare(dry_run=args.dry_run)
    print(json.dumps(r2, indent=2))

    print("\n[3/3] Prod admin DB (admin_crm.mockup_approvals)...")
    r3 = purge_prod_admin_db(dry_run=args.dry_run)
    print(json.dumps(r3, indent=2))

    print("\n" + "=" * 60)
    if args.dry_run:
        print("DRY RUN — nothing was deleted. Re-run without --dry-run to proceed.")
    else:
        print("PURGE COMPLETE")
    print("=" * 60)


if __name__ == "__main__":
    main()
