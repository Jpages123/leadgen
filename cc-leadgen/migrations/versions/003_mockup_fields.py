"""Add mockup generation fields to leads table.

Revision ID: 003_mockup_fields
Revises: 002_web_audit_fields
Create Date: 2026-06-28
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "003_mockup_fields"
down_revision: Union[str, None] = "002_web_audit_fields"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("leads", sa.Column("mockup_url", sa.Text(), nullable=True))
    op.add_column("leads", sa.Column("mockup_generated_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column(
        "leads",
        sa.Column(
            "mockup_status",
            sa.String(30),
            nullable=False,
            server_default="none",
        ),
    )
    op.create_index("ix_leads_mockup_status", "leads", ["mockup_status"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_leads_mockup_status", table_name="leads")
    op.drop_column("leads", "mockup_status")
    op.drop_column("leads", "mockup_generated_at")
    op.drop_column("leads", "mockup_url")
