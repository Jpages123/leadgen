"""Add leads.claimed_until for the cross-worker follow-up claim pattern.

With Celery worker concurrency=12, two beat ticks can land within seconds of
each other. The pre-existing `_blocked()` check guards against a single lead
getting the SAME step sent twice (a step-2 row blocks further step-2 sends
for that lead), but it does not prevent two workers from sending DIFFERENT
candidates within seconds — collapsing the within-day pacing to ~0.

This migration adds a single nullable column used as a short-lived atomic
claim. Before each send, the worker issues:

    UPDATE leads
       SET claimed_until = NOW() + INTERVAL '5 minutes'
     WHERE id = $1
       AND (claimed_until IS NULL OR claimed_until < NOW())
    RETURNING id;

If 0 rows are returned, another worker holds the claim for this lead and the
candidate is skipped. The TTL covers a normal SMTP send + OutreachSequence
write; if a worker crashes mid-send the claim expires and the next worker can
retry (the `_blocked` check then prevents a duplicate row from being written).

The claim is at the lead level, not (lead, step). Two workers can't
realistically send different steps for the same lead simultaneously because
later steps only become eligible many hours after earlier ones, so lead-level
serialisation is sufficient.

Revision ID: 012_followup_claim
Revises: 011_mockup_retry_count
Create Date: 2026-08-24
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "012_followup_claim"
down_revision: Union[str, None] = "011_mockup_retry_count"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "leads",
        sa.Column("claimed_until", sa.DateTime(timezone=True), nullable=True),
    )
    # Partial index — only rows with an active claim. Cheap, keeps the working
    # set tiny in practice (most leads are NULL).
    op.create_index(
        "ix_leads_claimed_until",
        "leads",
        ["claimed_until"],
        postgresql_where=sa.text("claimed_until IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_index("ix_leads_claimed_until", table_name="leads")
    op.drop_column("leads", "claimed_until")
