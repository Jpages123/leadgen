"""Stub migration — mockup_contact_gate (columns already in DB, file missing from repo).

Revision ID: 006_mockup_contact_gate
Revises: 005_scoring_signals
Create Date: 2026-07-11
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "006_mockup_contact_gate"
down_revision: Union[str, None] = "005_scoring_signals"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    conn = op.get_bind()
    result = conn.execute(sa.text(
        "SELECT 1 FROM information_schema.columns WHERE table_name='leads' AND column_name='mockup_eligible_pending_contact'"
    ))
    if not result.fetchone():
        op.add_column("leads", sa.Column("mockup_eligible_pending_contact", sa.Boolean(), nullable=True))


def downgrade() -> None:
    op.drop_column("leads", "mockup_eligible_pending_contact")
