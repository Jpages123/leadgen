"""Rejected websites model — franchises / multi-location leads to skip during discovery."""
from __future__ import annotations

from datetime import datetime
from typing import Optional

from sqlalchemy import DateTime, String
from sqlalchemy.sql import func
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, Timestamps, UUIDPrimaryKey


class RejectedWebsite(Base, UUIDPrimaryKey, Timestamps):
    """A website domain or URL to filter out during lead discovery.

    Use this for franchise leads where multiple Google Maps listings (one per
    location) all point to the same franchise website. One entry here blocks
    all locations from being added.
    """
    __tablename__ = "rejected_websites"

    website: Mapped[str] = mapped_column(
        String(500),
        nullable=False,
        unique=True,
        index=True,
        comment="Domain or full URL. e.g. 'example.com' or 'https://example.com/franchise'",
    )
    reason: Mapped[Optional[str]] = mapped_column(
        String(255),
        nullable=True,
        comment="Why it was rejected. E.g. 'Franchise lead — all locations share same site'",
    )
    rejected_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        comment="When the rejection was added",
    )
