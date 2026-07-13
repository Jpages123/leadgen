"""Add mockup_targeted flag to leads table.

Used by /admin/mockup-lead-selection to surface operator-curated leads
that should be regenerated (e.g. after the purge or for follow-ups).

When at least one lead has mockup_targeted=TRUE, the SELECT page shows
ONLY those (with a 'show all' fallback). When none have it, falls back
to the current behavior (all leads scoring >= 70 with mockup_status='none').

Revision ID: 008_mockup_targeted
Revises: 007_svc_text
Create Date: 2026-07-13
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "008_mockup_targeted"
down_revision: Union[str, None] = "003_mockup_fields"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "leads",
        sa.Column(
            "mockup_targeted",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
    )
    op.create_index(
        "ix_leads_mockup_targeted",
        "leads",
        ["mockup_targeted"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_leads_mockup_targeted", table_name="leads")
    op.drop_column("leads", "mockup_targeted")
