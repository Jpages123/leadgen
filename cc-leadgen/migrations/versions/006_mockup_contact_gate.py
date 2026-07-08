"""006_mockup_contact_gate: gate mockup generation on lead contact info.

Phase: 2026-07-08 Tier-1 fix (audit follow-up).
Problem: web_audit.py queues generate_mockup whenever pitch_score ≥ threshold,
even when the lead has no email/phone/whatsapp. We deploy mockups for leads
we cannot contact — pure waste of Cloudflare Pages builds.

Fix:
  1. web_audit only queues mockup if pitch ≥ threshold AND lead has at least
     one contact channel (email OR phone OR whatsapp_number).
  2. If pitch ≥ threshold but no contact info, set
     `mockup_eligible_pending_contact = true` instead.
  3. enrichment workers check this flag after they fill in contact info, and
     queue the deferred mockup if pitch still ≥ threshold.
  4. mockup_generator has a defensive top-of-function check that bails out
     cleanly if no contact info (defence in depth against manual re-triggers).

Adds 1 column:
  mockup_eligible_pending_contact  BOOLEAN  — audit flagged this lead as
                                              mockup-worthy but couldn't
                                              contact yet; re-check after
                                              enrichment fills a channel.

Revision ID: 006_mockup_contact_gate
Revises:    005_scoring_signals
Create Date: 2026-07-08
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
    op.add_column(
        "leads",
        sa.Column(
            "mockup_eligible_pending_contact",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
    )
    # Partial index — admin queries for flagged leads awaiting contact info
    op.create_index(
        "ix_leads_mockup_pending_contact",
        "leads",
        ["mockup_eligible_pending_contact"],
        unique=False,
        postgresql_where=sa.text("mockup_eligible_pending_contact = true"),
    )


def downgrade() -> None:
    op.drop_index("ix_leads_mockup_pending_contact", table_name="leads")
    op.drop_column("leads", "mockup_eligible_pending_contact")