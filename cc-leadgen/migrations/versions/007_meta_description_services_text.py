"""007: add meta_description and services_text to leads.

Session B improvement: richer LLM copy generation hints.
- meta_description: scraped from <meta name="description"> tag
- services_text:    extracted from body copy (services/about sections)
Both are passed as existing_services hints to generate_copy().
"""
from __future__ import annotations
from typing import Union

from alembic import op
import sqlalchemy as sa

revision: str = "007_svc_text"
down_revision: Union[str, None] = "006_mockup_contact_gate"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("leads", sa.Column("meta_description", sa.Text(), nullable=True))
    op.add_column("leads", sa.Column("services_text", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("leads", "services_text")
    op.drop_column("leads", "meta_description")
