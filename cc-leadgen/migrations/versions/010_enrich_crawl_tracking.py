"""Add enrich_crawl_attempted_at / enrich_crawl_failed to leads.

Without these, enrich_website_crawl() selected leads purely on
`status='discovered' AND website IS NOT NULL AND email IS NULL` with no
ORDER BY and no record of prior attempts. Leads whose site is down /
unreachable never get an email extracted (so the filter never excludes
them) and, lacking an ORDER BY, Postgres kept handing back the same
~20 dead URLs on every 15-minute beat tick instead of ever advancing to
the rest of the backlog.

Revision ID: 010_enrich_crawl_tracking
Revises: 009_rejected_websites
Create Date: 2026-08-02
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "010_enrich_crawl_tracking"
down_revision: Union[str, None] = "009_rejected_websites"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "leads",
        sa.Column("enrich_crawl_attempted_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "leads",
        sa.Column("enrich_crawl_failed", sa.Boolean(), nullable=False, server_default=sa.false()),
    )


def downgrade() -> None:
    op.drop_column("leads", "enrich_crawl_failed")
    op.drop_column("leads", "enrich_crawl_attempted_at")
