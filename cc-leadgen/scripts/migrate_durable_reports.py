"""One-shot migration: move surviving PDFs from /tmp/cc_reports/ to .cache/reports/.

Background
----------
Session 15 (2026-07-15) introduced durable PDF storage at
``<project>/.cache/reports/``. At the time of this script:

  - 54 PDFs still exist in ``/tmp/cc_reports/`` (the legacy location)
  - 2,202 leads in leadgen DB have ``web_audit_pdf_path`` pointing at
    the old /tmp paths (most of which are now gone)
  - The new ``report_assets.resolve()`` transparently falls back to
    /tmp for any pre-fix DB rows, so we don't strictly *need* to
    migrate these files. But moving them shortens the read path on
    every future send.

This script:

  1. Copies every ``/tmp/cc_reports/*.pdf`` to the durable location
     (idempotent — skips if the durable copy already exists).
  2. Updates each lead's ``web_audit_pdf_path`` column to point at the
     new durable path (so future reads hit the fast path).
  3. Reports counts and skips.

Run with::

    cd ~/installedApps/leadgen/cc-leadgen && source .venv/bin/activate \\
        && python scripts/migrate_durable_reports.py
"""
from __future__ import annotations

import shutil
import sys
from pathlib import Path

# Allow `python scripts/migrate_durable_reports.py` from project root
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from app.db.sync_session import sync_session_scope  # noqa: E402
from app.models import Lead  # noqa: E402
from app.utils.report_assets import report_dir, _slug_from_name  # noqa: E402
from app.utils.logger import get_logger  # noqa: E402

log = get_logger(__name__)


def main():
    durable = report_dir()
    legacy = Path("/tmp/cc_reports")

    if not legacy.exists():
        print(f"No legacy dir at {legacy} — nothing to migrate.")
        return

    legacy_pdfs = sorted(legacy.glob("*.pdf"))
    print(f"Found {len(legacy_pdfs)} PDFs in legacy {legacy}")
    print(f"Migrating to durable {durable}")
    print()

    moved = 0
    skipped = 0
    db_updated = 0

    for legacy_path in legacy_pdfs:
        slug = legacy_path.stem
        dest = durable / legacy_path.name
        if dest.exists() and dest.stat().st_size > 0:
            skipped += 1
            continue

        # Copy first (cheap), update DB second (slower).
        try:
            shutil.copy2(legacy_path, dest)
            moved += 1
            log.info("pdf_migrated", slug=slug, dest=str(dest))
        except Exception as exc:
            log.warning("pdf_copy_failed", slug=slug, error=str(exc))
            continue

        # Update DB row(s) where business_name slug matches.
        with sync_session_scope() as session:
            # Find leads whose business_name slugifies to this slug.
            # We can't query by computed column directly — iterate leads
            # whose web_audit_pdf_path currently points at this /tmp file.
            candidates = session.query(Lead).filter(
                Lead.web_audit_pdf_path == str(legacy_path)
            ).all()
            for lead in candidates:
                lead.web_audit_pdf_path = str(dest)
                db_updated += 1

    print(f"Migrated : {moved}")
    print(f"Skipped  : {skipped} (already in durable)")
    print(f"DB rows updated to point at durable path : {db_updated}")


if __name__ == "__main__":
    main()