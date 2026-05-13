"""Outreach sequence model — tracks each touch in an email/WhatsApp sequence."""
from __future__ import annotations

from typing import Optional

import uuid
from sqlalchemy import DateTime, ForeignKey, Integer, String, Text, Uuid
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, Timestamps, UUIDPrimaryKey


class OutreachSequence(Base, UUIDPrimaryKey, Timestamps):
    __tablename__ = "outreach_sequences"

    lead_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("leads.id"), nullable=False, index=True)
    channel: Mapped[str] = mapped_column(String(20), nullable=False)  # email, whatsapp
    sequence_name: Mapped[str] = mapped_column(String(100), nullable=False)
    step_number: Mapped[int] = mapped_column(Integer, nullable=False)

    scheduled_at: Mapped[Optional[str]] = mapped_column(DateTime(timezone=True), nullable=True)
    sent_at: Mapped[Optional[str]] = mapped_column(DateTime(timezone=True), nullable=True)

    status: Mapped[str] = mapped_column(
        String(20),
        nullable=False,
        default="pending",
        index=True,
    )
    # pending, sent, failed, bounced, replied, skipped

    subject: Mapped[Optional[str]] = mapped_column(Text, nullable=True)  # email only
    message_body: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    error_message: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    # Email tracking
    message_id: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)  # SMTP Message-ID for reply matching
    unsubscribed: Mapped[bool] = mapped_column(default=False, nullable=False)

    # Relationship
    lead: Mapped["Lead"] = relationship(back_populates="sequences")

    def __repr__(self) -> str:
        return f"<OutreachSequence {self.id} step={self.step_number} status={self.status}>"