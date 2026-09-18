"""004_mockup_assets: add scraped assets + URL quality fields to leads.

Adds columns populated by web scraper (Phase A upgrade to web_audit.py):
  scraped_logo_path      TEXT          - local path to downloaded logo image
  scraped_hero_path      TEXT          - local path to downloaded hero image
  scraped_gallery_paths  JSONB         - list of local paths (gallery)
  scraped_brand_color    VARCHAR(7)    - hex like '#1e9be8'
  needs_url_review       BOOLEAN       - URL points to booking/social platform
  url_quality_issue      VARCHAR(100)  - reason: 'fresha', 'tiktok', etc.
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "004_mockup_assets"
down_revision: Union[str, None] = "003_mockup_fields"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("leads", sa.Column("scraped_logo_path", sa.Text(), nullable=True))
    op.add_column("leads", sa.Column("scraped_hero_path", sa.Text(), nullable=True))
    op.add_column("leads", sa.Column("scraped_gallery_paths", sa.dialects.postgresql.JSONB(), nullable=True))
    op.add_column("leads", sa.Column("scraped_brand_color", sa.String(7), nullable=True))
    op.add_column(
        "leads",
        sa.Column(
            "needs_url_review",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
    )
    op.add_column("leads", sa.Column("url_quality_issue", sa.String(100), nullable=True))


def downgrade() -> None:
    op.drop_column("leads", "url_quality_issue")
    op.drop_column("leads", "needs_url_review")
    op.drop_column("leads", "scraped_brand_color")
    op.drop_column("leads", "scraped_gallery_paths")
    op.drop_column("leads", "scraped_hero_path")
    op.drop_column("leads", "scraped_logo_path")