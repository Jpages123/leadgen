#!/bin/bash
# Run purge inside the worker container (where 'postgres' host resolves).
set -e

cd ~/installedApps/leadgen/cc-leadgen

echo "=== Step 1: Mark Limelight sibling duplicate as invalid ==="
docker compose exec -T worker .venv/bin/python <<'PYEOF'
from app.db.sync_session import sync_session_scope
from app.models.lead import Lead
from sqlalchemy import update
with sync_session_scope() as s:
    sib_id = "adb75a0d-b5c1-4af2-99e6-f1b3454e820a"
    stmt = update(Lead).where(Lead.id == sib_id).values(
        status="invalid",
        audit_error="duplicate_sibling_of_eb3ec99f",
        mockup_status="none",
        mockup_url=None,
    )
    result = s.execute(stmt)
    print(f"  Sibling marked invalid: {result.rowcount} rows")
PYEOF

echo ""
echo "=== Step 2: Dry-run purge ==="
docker compose exec -T worker .venv/bin/python -m scripts.purge_all_mockups --dry-run

echo ""
echo "=== Step 3: Live purge ==="
docker compose exec -T worker .venv/bin/python -m scripts.purge_all_mockups

echo ""
echo "=== Step 4: Verify post-state ==="
docker compose exec -T worker .venv/bin/python <<'PYEOF'
from app.db.sync_session import sync_session_scope
from app.models.lead import Lead
from sqlalchemy import select, func
with sync_session_scope() as s:
    rows = s.execute(
        select(Lead.mockup_status, func.count())
        .where(Lead.mockup_status.isnot(None))
        .group_by(Lead.mockup_status)
    ).all()
    print("  mockup_status counts after purge:")
    for status, n in rows:
        print(f"    {status}: {n}")
    n_url = s.execute(select(func.count()).where(Lead.mockup_url.isnot(None))).scalar()
    print(f"  leads_with_url: {n_url}")
PYEOF
