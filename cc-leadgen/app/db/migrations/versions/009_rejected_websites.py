"""Add rejected_websites table.

Used by discovery.py to filter out franchise / multi-location leads that
share a single website. If a newly discovered lead's website domain matches
an entry in this table, the lead is skipped and not inserted into the DB.

Revision ID: 009_rejected_websites
Revises: 008_mockup_targeted
Create Date: 2026-07-19
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "009_rejected_websites"
down_revision: Union[str, None] = "007_svc_text"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "rejected_websites",
        sa.Column("id", sa.UUID(), nullable=False, server_default=sa.text("gen_random_uuid()")),
        sa.Column(
            "website",
            sa.String(500),
            nullable=False,
            comment="Domain or full URL to reject. e.g. 'example.com' or 'https://example.com/franchise'",
        ),
        sa.Column("reason", sa.String(255), nullable=True, comment="Why it was rejected, e.g. 'Franchise lead'"),
        sa.Column("rejected_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("NOW()")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("NOW()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("NOW()")),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_rejected_websites_website", "rejected_websites", ["website"], unique=True)


def downgrade() -> None:
    op.drop_index("ix_rejected_websites_website", table_name="rejected_websites")
    op.drop_table("rejected_websites")
