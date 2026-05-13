"""Discovery job tracking — records each discovery run's metadata and results."""
from __future__ import annotations

from typing import Optional

from sqlalchemy import Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, Timestamps, UUIDPrimaryKey


class DiscoveryJob(Base, UUIDPrimaryKey, Timestamps):
    __tablename__ = "discovery_jobs"

    source: Mapped[str] = mapped_column(String(50), nullable=False)  # google_maps, yellsa, cylex
    query: Mapped[str] = mapped_column(String(500), nullable=False)  # e.g. "hair salon Cape Town"

    status: Mapped[str] = mapped_column(
        String(20),
        nullable=False,
        default="running",
        index=True,
    )
    # running, done, failed

    leads_found: Mapped[int] = mapped_column(Integer, default=0)
    leads_new: Mapped[int] = mapped_column(Integer, default=0)  # deduped count

    started_at: Mapped[Optional[str]] = mapped_column(String(30), nullable=True)
    completed_at: Mapped[Optional[str]] = mapped_column(String(30), nullable=True)
    error_message: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    def __repr__(self) -> str:
        return f"<DiscoveryJob {self.id} {self.source} {self.query} status={self.status}>"