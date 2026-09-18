"""App settings model — small key/value store for admin-toggleable feature flags.

First consumer: `discovery_enabled`, added when the Google Places API budget
started running low (2026-08-28) and there was no way to pause the discovery
beat task without editing code. The admin portal writes directly to this
table over the existing cross-Tailscale `leadgenQuery` write path; workers
read it at the top of the relevant task.
"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, String
from sqlalchemy.sql import func
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class AppSetting(Base):
    """A single key/value setting row. Values are always stored as strings —
    callers are responsible for parsing (e.g. `value == 'true'`)."""
    __tablename__ = "app_settings"

    key: Mapped[str] = mapped_column(String(100), primary_key=True)
    value: Mapped[str] = mapped_column(String(500), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )
