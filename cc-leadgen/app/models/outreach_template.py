"""Outreach template model — stores message templates for each vertical/channel."""
from __future__ import annotations

from typing import Optional

from sqlalchemy import Boolean, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, Timestamps, UUIDPrimaryKey


class OutreachTemplate(Base, UUIDPrimaryKey, Timestamps):
    __tablename__ = "outreach_templates"

    name: Mapped[str] = mapped_column(String(255), unique=True, nullable=False)
    channel: Mapped[str] = mapped_column(String(20), nullable=False)  # email, whatsapp
    vertical: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)  # NULL = all verticals
    step: Mapped[int] = mapped_column(Integer, nullable=False)  # 1, 2, 3 in a sequence

    subject: Mapped[Optional[str]] = mapped_column(Text, nullable=True)  # email only
    body: Mapped[str] = mapped_column(Text, nullable=False)

    active: Mapped[bool] = mapped_column(Boolean, default=True)
    version: Mapped[int] = mapped_column(Integer, default=1)  # for A/B testing

    # A/B test assignment
    ab_variant: Mapped[Optional[str]] = mapped_column(String(5), nullable=True)  # 'A' or 'B'

    def __repr__(self) -> str:
        return f"<OutreachTemplate {self.name} v{self.version}>"