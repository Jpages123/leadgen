"""Add mockup_retry_count to leads for immediate-retry rate limiting.

Without this, the new retry_single_mockup task would re-dispatch a failed
mockup indefinitely. With this column + MOCKUP_MAX_AUTO_RETRIES (default 3),
we stop auto-retrying after N failures per lead and let the operator decide
whether to manually re-queue.

Counter is incremented inside retry_single_mockup (not on every failure),
so it counts *retries*, not total failures. A lead that fails on the first
attempt and succeeds on the first retry has mockup_retry_count=1.

Revision ID: 011_mockup_retry_count
Revises: 010_enrich_crawl_tracking
Create Date: 2026-08-03
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "011_mockup_retry_count"
down_revision: Union[str, None] = "010_enrich_crawl_tracking"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "leads",
        sa.Column(
            "mockup_retry_count",
            sa.Integer(),
            nullable=False,
            server_default="0",
        ),
    )


def downgrade() -> None:
    op.drop_column("leads", "mockup_retry_count")
