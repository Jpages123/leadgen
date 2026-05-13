"""Lead model — central entity for all lead data."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import Boolean, DateTime, Integer, Numeric, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, Timestamps, UUIDPrimaryKey


class Lead(Base, UUIDPrimaryKey, Timestamps):
    __tablename__ = "leads"

    # Source identification
    source: Mapped[str] = mapped_column(String(50), nullable=False)  # google_maps, yellsa, cylex, instagram, manual
    source_url: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    # Business details
    business_name: Mapped[str] = mapped_column(String(255), nullable=False)
    owner_name: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    phone: Mapped[Optional[str]] = mapped_column(String(30), nullable=True)  # E.164
    whatsapp_number: Mapped[Optional[str]] = mapped_column(String(30), nullable=True)
    email: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    website: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    # Social presence
    instagram_url: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    facebook_url: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    # Location
    city: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    province: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)

    # Business classification
    business_type: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)

    # Yep Mall enrichment
    seller_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    detail_fetched: Mapped[bool] = mapped_column(Boolean, default=False)

    # Google signals
    google_rating: Mapped[Optional[float]] = mapped_column(Numeric(2, 1), nullable=True)
    google_review_count: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)

    # Scoring
    score: Mapped[int] = mapped_column(Integer, default=0)

    # Lifecycle status
    status: Mapped[str] = mapped_column(
        String(50),
        nullable=False,
        default="discovered",
        index=True,
    )
    # Status values:
    # discovered → enriched → outreach_queued
    # → contacted → responded → interested
    # → converted | not_interested | opted_out | invalid | competitor_customer

    # Outreach tracking
    outreach_channel: Mapped[Optional[str]] = mapped_column(String(20), nullable=True)  # email, whatsapp, both

    # CRM sync
    synced_to_crm: Mapped[bool] = mapped_column(Boolean, default=False)
    crm_lead_id: Mapped[Optional[str]] = mapped_column(String(50), nullable=True)

    # Notes
    notes: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    # Timestamps for outreach cadence
    discovered_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    last_contacted_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    next_follow_up_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)

    # Relationships
    sequences: Mapped[list["OutreachSequence"]] = relationship(back_populates="lead", cascade="all, delete-orphan")
    events: Mapped[list["LeadEvent"]] = relationship(back_populates="lead", cascade="all, delete-orphan")

    def __repr__(self) -> str:
        return f"<Lead {self.id} {self.business_name} status={self.status}>"