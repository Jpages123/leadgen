"""Stub migration — add meta_description and services_text to leads (file missing from repo).

Revision ID: 007_svc_text
Revises: 006_mockup_contact_gate
Create Date: 2026-07-13
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "007_svc_text"
down_revision: Union[str, None] = "006_mockup_contact_gate"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    conn = op.get_bind()
    for col_name, col_type in [("meta_description", sa.Text()), ("services_text", sa.Text())]:
        result = conn.execute(sa.text(
            f"SELECT 1 FROM information_schema.columns WHERE table_name='leads' AND column_name='{col_name}'"
        ))
        if not result.fetchone():
            op.add_column("leads", sa.Column(col_name, col_type, nullable=True))


def downgrade() -> None:
    op.drop_column("leads", "meta_description")
    op.drop_column("leads", "services_text")
