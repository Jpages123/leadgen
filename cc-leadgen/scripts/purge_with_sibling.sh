#!/bin/bash
# Mark Limelight sibling duplicate as invalid.
set -e
cd ~/installedApps/leadgen/cc-leadgen

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
