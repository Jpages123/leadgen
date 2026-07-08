"""005_scoring_signals: extend leadgen scoring with modern-web signals.

Phase: 2026-07-08 scoring audit (Mnemosyne review of 3 pending mockup leads).
Findings:
  - Ground Up Construction: HTTP-only, no WhatsApp, no schema, no analytics
    were never captured as scoring signals
  - Maboneng Photoshoot Studios: godaddy platform flat-25 score over-rated
    recent GoDaddy Builder 8.0 sites
  - Belle Doux Events: modern Wix with HTTPS + JSON-LD + analytics still
    scored 70 — needs a dampener for "modern stack" sites

Adds 7 columns populated by the web scraper:
  is_https               BOOLEAN  — site serves over HTTPS
  has_whatsapp           BOOLEAN  — site has a wa.me / whatsapp:// link
  has_jsonld             BOOLEAN  — site has JSON-LD structured data
  has_analytics          BOOLEAN  — site loads gtag / GA / FB Pixel
  has_manifest           BOOLEAN  — site has a PWA web manifest
  h1_text                TEXT     — the actual <h1> text (for generic-H1 detection)
  has_unedited_template  BOOLEAN  — site contains known template-leftover copy
  pitch_breakdown        JSONB    — score component breakdown for admin tooltip

Revision ID: 005_scoring_signals
Revises:    004_mockup_assets
Create Date: 2026-07-08
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "005_scoring_signals"
down_revision: Union[str, None] = "004_mockup_assets"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "leads",
        sa.Column("is_https", sa.Boolean(), nullable=False, server_default=sa.text("false")),
    )
    op.add_column(
        "leads",
        sa.Column("has_whatsapp", sa.Boolean(), nullable=False, server_default=sa.text("false")),
    )
    op.add_column(
        "leads",
        sa.Column("has_jsonld", sa.Boolean(), nullable=False, server_default=sa.text("false")),
    )
    op.add_column(
        "leads",
        sa.Column("has_analytics", sa.Boolean(), nullable=False, server_default=sa.text("false")),
    )
    op.add_column(
        "leads",
        sa.Column("has_manifest", sa.Boolean(), nullable=False, server_default=sa.text("false")),
    )
    op.add_column("leads", sa.Column("h1_text", sa.Text(), nullable=True))
    op.add_column(
        "leads",
        sa.Column("has_unedited_template", sa.Boolean(), nullable=False, server_default=sa.text("false")),
    )
    op.add_column("leads", sa.Column("pitch_breakdown", sa.dialects.postgresql.JSONB(), nullable=True))

    # Indexes for fast filtering — the admin mockup-approvals page sorts
    # by score and these signals drive the dampener explainability
    op.create_index("ix_leads_is_https", "leads", ["is_https"], unique=False)
    op.create_index("ix_leads_has_whatsapp", "leads", ["has_whatsapp"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_leads_has_whatsapp", table_name="leads")
    op.drop_index("ix_leads_is_https", table_name="leads")
    op.drop_column("leads", "pitch_breakdown")
    op.drop_column("leads", "has_unedited_template")
    op.drop_column("leads", "h1_text")
    op.drop_column("leads", "has_manifest")
    op.drop_column("leads", "has_analytics")
    op.drop_column("leads", "has_jsonld")
    op.drop_column("leads", "has_whatsapp")
    op.drop_column("leads", "is_https")