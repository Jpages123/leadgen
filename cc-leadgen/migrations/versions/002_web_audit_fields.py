"""Add web audit fields to leads table.

Revision ID: 002_web_audit_fields
Revises: 001_initial
Create Date: 2026-06-28
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "002_web_audit_fields"
down_revision: Union[str, None] = "001_initial"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("leads", sa.Column("website_platform", sa.String(50), nullable=True))
    op.add_column("leads", sa.Column("pagespeed_mobile", sa.Integer(), nullable=True))
    op.add_column("leads", sa.Column("pagespeed_desktop", sa.Integer(), nullable=True))
    op.add_column("leads", sa.Column("pagespeed_seo", sa.Integer(), nullable=True))
    op.add_column("leads", sa.Column("pagespeed_a11y", sa.Integer(), nullable=True))
    op.add_column("leads", sa.Column("site_copyright_year", sa.Integer(), nullable=True))
    op.add_column("leads", sa.Column("web_audit_generated_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("leads", sa.Column("web_audit_pdf_path", sa.Text(), nullable=True))
    op.add_column("leads", sa.Column("web_audit_screenshot_path", sa.Text(), nullable=True))
    op.add_column("leads", sa.Column("web_pitch_score", sa.Integer(), nullable=True))
    op.add_column("leads", sa.Column("seller_id", sa.Integer(), nullable=True))
    op.add_column("leads", sa.Column("detail_fetched", sa.Boolean(), nullable=False, server_default=sa.text("FALSE")))

    op.create_index("ix_leads_web_pitch_score", "leads", [sa.text("web_pitch_score")], unique=False)
    op.create_index("ix_leads_website_platform", "leads", [sa.text("website_platform")], unique=False)


def downgrade() -> None:
    op.drop_index("ix_leads_web_pitch_score", table_name="leads")
    op.drop_index("ix_leads_website_platform", table_name="leads")
    op.drop_column("leads", "detail_fetched")
    op.drop_column("leads", "seller_id")
    op.drop_column("leads", "web_pitch_score")
    op.drop_column("leads", "web_audit_screenshot_path")
    op.drop_column("leads", "web_audit_pdf_path")
    op.drop_column("leads", "web_audit_generated_at")
    op.drop_column("leads", "site_copyright_year")
    op.drop_column("leads", "pagespeed_a11y")
    op.drop_column("leads", "pagespeed_seo")
    op.drop_column("leads", "pagespeed_desktop")
    op.drop_column("leads", "pagespeed_mobile")
    op.drop_column("leads", "website_platform")
