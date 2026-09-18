"""Add app_settings — small key/value table for admin-toggleable feature flags.

First consumer: `discovery_enabled` (default 'true'). The Google Places API
budget started running low (2026-08-28), and there was no way to pause the
discovery beat task (`run_daily_discovery`, runs daily at
`discovery_schedule_hour`) short of editing code and redeploying. This adds
a generic settings table (rather than a single boolean column on some
existing model) so future toggles don't each need their own migration.

The admin portal (`login-portal/routes/admin.js`, Leadgen Flow page) writes
to this table directly via `leadgenQuery` — same cross-Tailscale write path
already used for mockup approvals / lead rejection. `run_daily_discovery`
reads it at the top of the task and short-circuits (no API calls, no spend)
when disabled.

Revision ID: 013_app_settings
Revises: 012_followup_claim
Create Date: 2026-08-28
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "013_app_settings"
down_revision: Union[str, None] = "012_followup_claim"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "app_settings",
        sa.Column("key", sa.String(100), primary_key=True),
        sa.Column("value", sa.String(500), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
    )
    op.execute(
        "INSERT INTO app_settings (key, value) VALUES ('discovery_enabled', 'true')"
    )


def downgrade() -> None:
    op.drop_table("app_settings")
