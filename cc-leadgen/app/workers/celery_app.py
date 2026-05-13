"""Celery application — workers and beat share this."""
from __future__ import annotations

from celery import Celery
from celery.schedules import crontab

from app.config import get_settings

settings = get_settings()

celery_app = Celery(
    "cc_leadgen",
    broker_url=settings.celery_broker_url,
    result_backend=settings.celery_result_backend,
    include=[
        "app.workers.discovery",
        "app.workers.enrichment",
        "app.workers.scoring",
        "app.workers.outreach",
        "app.workers.response",
    ],
)

# ── Celery Beat Schedule ───────────────────────────────────────────────
celery_app.conf.timezone = "Africa/Johannesburg"
celery_app.conf.beat_schedule = {
    # Phase 1 — Daily discovery sweep at 8am
    "discovery-daily": {
        "task": "app.workers.discovery.tasks.run_daily_discovery",
        "schedule": crontab(hour=settings.discovery_schedule_hour, minute=0),
        "kwargs": {"vertical": None},  # None = all verticals
    },
    # Phase 2 — Enrichment queue processor (every 15 min)
    "enrichment-process": {
        "task": "app.workers.enrichment.tasks.process_enrichment_queue",
        "schedule": crontab(minute="*/15"),
    },
    # Phase 3 — Queue enriched leads for outreach (every 2 hours)
    "outreach-queue-leads": {
        "task": "app.workers.outreach.tasks.queue_leads_for_outreach",
        "schedule": crontab(hour="*/2", minute=0),
        "kwargs": {"min_score": 40},
    },
    # Phase 3 — Send email sequences (every 30 min, respects daily cap)
    "outreach-send": {
        "task": "app.workers.outreach.tasks.send_email_sequence",
        "schedule": crontab(minute=settings.outreach_schedule_minute),
    },
    # Phase 4 — Email reply polling (every 15 min)
    "response-poll": {
        "task": "app.workers.outreach.tasks.check_replies",
        "schedule": crontab(minute="*/15"),
    },
}
celery_app.conf.task_routes = {
    "app.workers.discovery.*": {"queue": "discovery"},
    "app.workers.enrichment.*": {"queue": "enrichment"},
    "app.workers.scoring.*": {"queue": "scoring"},
    "app.workers.outreach.*": {"queue": "outreach"},
    "app.workers.response.*": {"queue": "response"},
}
celery_app.conf.task_acks_late = True
celery_app.conf.task_reject_on_worker_lost = True
