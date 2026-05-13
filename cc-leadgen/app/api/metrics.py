"""Basic metrics endpoint for monitoring."""
from __future__ import annotations

from fastapi import APIRouter
from sqlalchemy import func, select

from app.db.session import session_scope
from app.models import Lead, OutreachSequence

router = APIRouter(prefix="/metrics", tags=["metrics"])


@router.get("")
async def metrics_summary() -> dict:
    """Return funnel metrics snapshot."""
    async with session_scope() as session:
        total_leads = await session.scalar(select(func.count(Lead.id)))

        status_counts = {}
        for status in [
            "discovered", "enriched", "outreach_queued",
            "contacted", "responded", "interested",
            "not_interested", "opted_out", "invalid",
        ]:
            cnt = await session.scalar(
                select(func.count(Lead.id)).where(Lead.status == status)
            )
            status_counts[status] = cnt or 0

        emails_sent = await session.scalar(
            select(func.count(OutreachSequence.id)).where(
                OutreachSequence.channel == "email",
                OutreachSequence.status == "sent",
            )
        )
        emails_pending = await session.scalar(
            select(func.count(OutreachSequence.id)).where(
                OutreachSequence.channel == "email",
                OutreachSequence.status == "pending",
            )
        )
        synced_to_crm = await session.scalar(
            select(func.count(Lead.id)).where(Lead.synced_to_crm == True)  # noqa: E712
        )

    return {
        "total_leads": total_leads or 0,
        "status_breakdown": status_counts,
        "emails_sent": emails_sent or 0,
        "emails_pending": emails_pending or 0,
        "synced_to_crm": synced_to_crm or 0,
    }
