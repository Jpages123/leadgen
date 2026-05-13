"""Dashboard page routes — all HTML templates rendered via Jinja2."""
from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Query, Request
from fastapi.responses import HTMLResponse
from sqlalchemy import func, select

from app.db.session import session_scope
from app.models import DiscoveryJob, Lead, OutreachSequence

router = APIRouter(tags=["pages"])

# Jinja2Templates is instantiated in app/api/main.py and shared.
from app.api.main import templates  # noqa: F401, E402

STATUS_OPTIONS = [
    "discovered", "enriched", "outreach_queued",
    "contacted", "responded", "interested",
    "converted", "not_interested", "opted_out", "invalid",
]
SOURCE_OPTIONS = ["google_maps", "yellsa", "cylex", "instagram", "manual"]
SEQ_STATUS_OPTIONS = ["pending", "sent", "failed", "bounced", "replied", "skipped"]


# ── Dashboard ───────────────────────────────────────────────────────────────


@router.get("/", response_class=HTMLResponse)
async def dashboard_page(request: Request) -> HTMLResponse:
    """Home dashboard — funnel overview + charts."""
    async with session_scope() as session:
        status_rows = (
            await session.execute(
                select(Lead.status, func.count(Lead.id)).group_by(Lead.status)
            )
        ).all()
        status_counts = {r[0]: r[1] for r in status_rows}
        total = sum(status_counts.values())

        synced = await session.scalar(
            select(func.count(Lead.id)).where(Lead.synced_to_crm.is_(True))
        )
        emails_sent = await session.scalar(
            select(func.count(OutreachSequence.id)).where(
                OutreachSequence.channel == "email",
                OutreachSequence.status == "sent",
            )
        )
        interested = status_counts.get("interested", 0)
        running_jobs = await session.scalar(
            select(func.count(DiscoveryJob.id)).where(
                DiscoveryJob.status == "running"
            )
        )

        seven_days_ago = datetime.now(timezone.utc) - timedelta(days=7)
        trend_rows = (
            await session.execute(
                select(
                    func.date(Lead.discovered_at).label("day"),
                    func.count(Lead.id),
                )
                .where(Lead.discovered_at >= seven_days_ago)
                .group_by(func.date(Lead.discovered_at))
                .order_by(func.date(Lead.discovered_at))
            )
        ).all()
        discovery_trend = {str(r[0]): r[1] for r in trend_rows if r[0]}

    return templates.TemplateResponse(
        request,
        "dashboard.html",
        context={
            "totals": {
                "total": total,
                "outreach_queued": status_counts.get("outreach_queued", 0),
                "responded": status_counts.get("responded", 0),
                "interested": interested,
                "synced": synced or 0,
                "emails_sent": emails_sent or 0,
                "running_jobs": running_jobs or 0,
            },
            "status_counts": status_counts,
            "discovery_trend": discovery_trend,
        },
    )


@router.get("/metrics/partial", response_class=HTMLResponse)
async def metrics_partial(request: Request) -> HTMLResponse:
    """HTMX partial — just the metrics cards."""
    async with session_scope() as session:
        status_rows = (
            await session.execute(
                select(Lead.status, func.count(Lead.id)).group_by(Lead.status)
            )
        ).all()
        status_counts = {r[0]: r[1] for r in status_rows}
        total = sum(status_counts.values())

        synced = await session.scalar(
            select(func.count(Lead.id)).where(Lead.synced_to_crm.is_(True))
        )
        emails_sent = await session.scalar(
            select(func.count(OutreachSequence.id)).where(
                OutreachSequence.channel == "email",
                OutreachSequence.status == "sent",
            )
        )
        running_jobs = await session.scalar(
            select(func.count(DiscoveryJob.id)).where(
                DiscoveryJob.status == "running"
            )
        )

    return templates.TemplateResponse(
        request,
        "partials/metrics_cards.html",
        context={
            "totals": {
                "total": total,
                "outreach_queued": status_counts.get("outreach_queued", 0),
                "responded": status_counts.get("responded", 0),
                "interested": status_counts.get("interested", 0),
                "synced": synced or 0,
                "emails_sent": emails_sent or 0,
                "running_jobs": running_jobs or 0,
            },
        },
    )


# ── Leads ───────────────────────────────────────────────────────────────────


@router.get("/leads", response_class=HTMLResponse)
async def leads_page(
    request: Request,
    page: int = Query(1, ge=1),
    status: str = Query("", description="Filter by status"),
    source: str = Query("", description="Filter by source"),
    search: str = Query("", description="Search business_name, city, owner_name"),
) -> HTMLResponse:
    """Leads list — paginated (50/page), filterable by status + source + search."""
    async with session_scope() as session:
        count_q = select(func.count(Lead.id))
        if status:
            count_q = count_q.where(Lead.status == status)
        if source:
            count_q = count_q.where(Lead.source == source)
        if search:
            pattern = f"%{search}%"
            count_q = count_q.where(
                Lead.business_name.ilike(pattern)
                | Lead.city.ilike(pattern)
                | Lead.owner_name.ilike(pattern)
            )
        total = await session.scalar(count_q) or 0

        q = (
            select(Lead)
            .order_by(Lead.discovered_at.desc().nullslast())
            .offset((page - 1) * 50)
            .limit(50)
        )
        if status:
            q = q.where(Lead.status == status)
        if source:
            q = q.where(Lead.source == source)
        if search:
            pattern = f"%{search}%"
            q = q.where(
                Lead.business_name.ilike(pattern)
                | Lead.city.ilike(pattern)
                | Lead.owner_name.ilike(pattern)
            )
        result = await session.execute(q)
        leads = result.scalars().all()

    total_pages = max(1, math.ceil(total / 50))
    return templates.TemplateResponse(
        request,
        "leads.html",
        context={
            "leads": leads,
            "total": total,
            "page": page,
            "total_pages": total_pages,
            "current_status": status,
            "current_source": source,
            "current_search": search,
            "status_options": STATUS_OPTIONS,
            "source_options": SOURCE_OPTIONS,
        },
    )


# ── Sequences ────────────────────────────────────────────────────────────────


@router.get("/sequences", response_class=HTMLResponse)
async def sequences_page(
    request: Request,
    page: int = Query(1, ge=1),
    status: str = Query("", description="Filter by status"),
    channel: str = Query("", description="Filter by channel"),
) -> HTMLResponse:
    """Outreach sequences — paginated, filterable."""
    async with session_scope() as session:
        count_q = select(func.count(OutreachSequence.id))
        if status:
            count_q = count_q.where(OutreachSequence.status == status)
        if channel:
            count_q = count_q.where(OutreachSequence.channel == channel)
        total = await session.scalar(count_q) or 0

        q = (
            select(OutreachSequence, Lead)
            .outerjoin(Lead, OutreachSequence.lead_id == Lead.id)
            .order_by(OutreachSequence.created_at.desc())
            .offset((page - 1) * 50)
            .limit(50)
        )
        if status:
            q = q.where(OutreachSequence.status == status)
        if channel:
            q = q.where(OutreachSequence.channel == channel)
        result = await session.execute(q)
        rows = result.all()

        sequences = []
        for seq, lead in rows:
            seq._lead_name = lead.business_name if lead else None  # type: ignore[attr-defined]
            seq._lead_email = lead.email if lead else None  # type: ignore[attr-defined]
            sequences.append(seq)

    total_pages = max(1, math.ceil(total / 50))
    return templates.TemplateResponse(
        request,
        "sequences.html",
        context={
            "sequences": sequences,
            "total": total,
            "page": page,
            "total_pages": total_pages,
            "current_status": status,
            "current_channel": channel,
            "status_options": SEQ_STATUS_OPTIONS,
        },
    )


# ── Discovery ─────────────────────────────────────────────────────────────


@router.get("/discovery", response_class=HTMLResponse)
async def discovery_page(request: Request) -> HTMLResponse:
    """Discovery jobs — history + manual run controls."""
    async with session_scope() as session:
        result = await session.execute(
            select(DiscoveryJob)
            .order_by(DiscoveryJob.created_at.desc())
            .limit(50)
        )
        jobs = result.scalars().all()

    return templates.TemplateResponse(request, "discovery.html", context={"jobs": jobs})


@router.get("/discovery/partial", response_class=HTMLResponse)
async def discovery_partial(request: Request) -> HTMLResponse:
    """HTMX partial — just the job list table."""
    async with session_scope() as session:
        result = await session.execute(
            select(DiscoveryJob)
            .order_by(DiscoveryJob.created_at.desc())
            .limit(50)
        )
        jobs = result.scalars().all()

    return templates.TemplateResponse(
        request, "partials/discovery_job_list.html", context={"jobs": jobs}
    )


@router.post("/discovery/run", response_class=HTMLResponse)
async def run_discovery(request: Request) -> HTMLResponse:
    """Manually trigger a discovery job. Returns HTML for HTMX."""
    from app.workers.discovery import run_daily_discovery

    async with session_scope() as session:
        job = DiscoveryJob(
            source="manual",
            query="Manual trigger from dashboard",
            status="running",
            started_at=datetime.now(timezone.utc).isoformat()[:30],
        )
        session.add(job)
        await session.flush()
        job_id = str(job.id)
        await session.commit()

    try:
        run_daily_discovery.delay(job_id=job_id, vertical=None)
        status_result = "dispatched"
    except Exception as exc:
        status_result = f"dispatch_failed: {exc}"

    return templates.TemplateResponse(
        request,
        "partials/discovery_status.html",
        context={
            "job_id": job_id,
            "status": status_result,
            "leads_found": 0,
            "leads_new": 0,
        },
    )


# ── HTMX Partial Routes ─────────────────────────────────────────────────────


@router.get("/leads/partial", response_class=HTMLResponse)
async def leads_partial(
    request: Request,
    page: int = Query(1, ge=1),
    status: str = Query(""),
    source: str = Query(""),
    search: str = Query(""),
) -> HTMLResponse:
    """HTMX partial — just the table body + pagination row."""
    async with session_scope() as session:
        q = (
            select(Lead)
            .order_by(Lead.discovered_at.desc().nullslast())
            .offset((page - 1) * 50)
            .limit(50)
        )
        if status:
            q = q.where(Lead.status == status)
        if source:
            q = q.where(Lead.source == source)
        if search:
            pattern = f"%{search}%"
            q = q.where(
                Lead.business_name.ilike(pattern)
                | Lead.city.ilike(pattern)
                | Lead.owner_name.ilike(pattern)
            )
        result = await session.execute(q)
        leads = result.scalars().all()

        count_q = select(func.count(Lead.id))
        if status:
            count_q = count_q.where(Lead.status == status)
        if source:
            count_q = count_q.where(Lead.source == source)
        if search:
            pattern = f"%{search}%"
            count_q = count_q.where(
                Lead.business_name.ilike(pattern)
                | Lead.city.ilike(pattern)
                | Lead.owner_name.ilike(pattern)
            )
        total = await session.scalar(count_q) or 0

    total_pages = max(1, math.ceil(total / 50))
    return templates.TemplateResponse(
        request,
        "partials/lead_table_body.html",
        context={
            "leads": leads,
            "total": total,
            "page": page,
            "total_pages": total_pages,
            "current_status": status,
            "current_source": source,
            "current_search": search,
        },
    )


@router.get("/sequences/partial", response_class=HTMLResponse)
async def sequences_partial(
    request: Request,
    page: int = Query(1, ge=1),
    status: str = Query(""),
    channel: str = Query(""),
) -> HTMLResponse:
    """HTMX partial — just the sequence table body + pagination."""
    async with session_scope() as session:
        q = (
            select(OutreachSequence, Lead)
            .outerjoin(Lead, OutreachSequence.lead_id == Lead.id)
            .order_by(OutreachSequence.created_at.desc())
            .offset((page - 1) * 50)
            .limit(50)
        )
        if status:
            q = q.where(OutreachSequence.status == status)
        if channel:
            q = q.where(OutreachSequence.channel == channel)
        result = await session.execute(q)
        rows = result.all()

        sequences = []
        for seq, lead in rows:
            seq._lead_name = lead.business_name if lead else None  # type: ignore[attr-defined]
            seq._lead_email = lead.email if lead else None  # type: ignore[attr-defined]
            sequences.append(seq)

        count_q = select(func.count(OutreachSequence.id))
        if status:
            count_q = count_q.where(OutreachSequence.status == status)
        if channel:
            count_q = count_q.where(OutreachSequence.channel == channel)
        total = await session.scalar(count_q) or 0

    total_pages = max(1, math.ceil(total / 50))
    return templates.TemplateResponse(
        request,
        "partials/sequence_table_body.html",
        context={
            "sequences": sequences,
            "total": total,
            "page": page,
            "total_pages": total_pages,
            "current_status": status,
            "current_channel": channel,
        },
    )
