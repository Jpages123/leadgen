"""Lead event model — immutable event log for lead lifecycle changes."""
from __future__ import annotations

from typing import Optional

import uuid
from sqlalchemy import JSON, String, Uuid, ForeignKey
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, Timestamps, UUIDPrimaryKey


class LeadEvent(Base, UUIDPrimaryKey, Timestamps):
    __tablename__ = "lead_events"

    lead_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("leads.id"), nullable=False, index=True)
    event_type: Mapped[str] = mapped_column(String(50), nullable=False, index=True)
    # event_types: email_sent, email_opened, email_clicked, replied, opted_out,
    #              status_change, score_updated, enriched, synced_to_crm, whatsapp_sent

    payload: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)
    # Stores relevant data: e.g. {"old_status": "discovered", "new_status": "enriched"}

    # Relationship
    lead: Mapped["Lead"] = relationship(back_populates="events")

    def __repr__(self) -> str:
        return f"<LeadEvent {self.event_type} on lead={self.lead_id}>"