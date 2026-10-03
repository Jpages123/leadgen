"""Celery application — workers and beat share this."""
from __future__ import annotations

from celery import Celery
from celery.schedules import crontab
from celery.signals import worker_ready

from app.config import get_settings

from app.utils.logger import get_logger
logger = get_logger(__name__)

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
        "app.workers.web_audit",
        "app.workers.mockup_generator",
        "app.workers.mockup_approval_sync",
        "app.workers.mockup_regen",
        "app.workers.email_draft",
    ],
)

# ── Celery Beat Schedule ───────────────────────────────────────────────
celery_app.conf.timezone = "Africa/Johannesburg"
celery_app.conf.beat_schedule = {
    # Phase 1 — Daily discovery sweep at 8am
    "discovery-daily": {
        "task": "app.workers.discovery.tasks.run_daily_discovery",
        "schedule": crontab(hour=settings.discovery_schedule_hour, minute=0),
        "kwargs": {"vertical": None},
    },
    # Phase 2 — Enrichment queue processor (every 15 min)
    "enrichment-process": {
        "task": "app.workers.enrichment.tasks.process_enrichment_queue",
        "schedule": crontab(minute="*/15"),
    },
    # Phase 2b — Web audit batch (every 30 min)
    "web-audit-batch": {
        "task": "app.workers.web_audit.tasks.run_web_audit_batch",
        "schedule": crontab(minute="*/30"),
        "kwargs": {"batch_size": 200},
    },
    # Phase 3 — Queue enriched leads for outreach (every 2 hours)
    "outreach-queue-leads": {
        "task": "app.workers.outreach.tasks.queue_leads_for_outreach",
        "schedule": crontab(hour="*/2", minute=0),
        "kwargs": {"min_score": 40},
    },
    # Phase 3 — Send email sequences (every 30 min, respects daily cap)
    # DISABLED (2026-08-28): this is a no-review, direct-SMTP send path for
    # generic (non-web-revamp) leads — it kept drip-feeding the old
    # WhatsApp-pitch templates (default/hair_beauty/cleaning) out of a
    # ~130-lead pre-pivot backlog with zero operator review. The business
    # has pivoted to web-revamp only, and all outreach now goes through the
    # reviewed draft flow (app.workers.email_draft). See send_email_sequence's
    # docstring for the incident writeup. Leave commented out unless a
    # reviewed path is built for generic leads.
    # "outreach-send": {
    #     "task": "app.workers.outreach.tasks.send_email_sequence",
    #     "schedule": crontab(minute=settings.outreach_schedule_minute),
    # },
    # Phase 4 — Email reply polling (every 15 min)
    "response-poll": {
        "task": "app.workers.outreach.tasks.check_replies",
        "schedule": crontab(minute="*/15"),
    },
    # Phase M — Follow-up sequences for web-revamp leads that haven't
    # replied (step 2 ~4 days, step 3 ~9 days after initial send).
    "outreach-followups": {
        "task": "app.workers.outreach.tasks.send_follow_up_sequence",
        "schedule": crontab(minute="*/30"),
    },
    # Phase B+ — Sync mockup approvals from prod DB (every 5 min)
    "mockup-approval-sync": {
        "task": "app.workers.mockup_approval_sync.tasks.sync_approvals",
        "schedule": crontab(minute="*/5"),
    },
    # Phase B+ — Retry mockup generation for leads stuck at mockup_status='failed'.
    # Queue is button-driven; this only handles transient build failures.
    "mockup-retry-sweep": {
        "task": "app.workers.mockup_regen.tasks.retry_failed_mockups",
        "schedule": crontab(minute="*/30"),
        "kwargs": {"batch_size": 20},
    },
    # Social publishing moved to the standalone social-publisher service
    # (~/installedApps/social-publisher) — see whatsapp_bot
    # docs/PLAN_SOCIAL_PUBLISHER.md.
}
celery_app.conf.task_routes = {
    "app.workers.discovery.*": {"queue": "discovery"},
    "app.workers.enrichment.*": {"queue": "enrichment"},
    "app.workers.scoring.*": {"queue": "scoring"},
    "app.workers.outreach.*": {"queue": "outreach"},
    "app.workers.response.*": {"queue": "response"},
    "app.workers.web_audit.*": {"queue": "enrichment"},
    "app.workers.mockup_generator.*": {"queue": "enrichment"},
    "app.workers.mockup_approval_sync.*": {"queue": "enrichment"},
}
celery_app.conf.task_acks_late = True
celery_app.conf.task_reject_on_worker_lost = True


@worker_ready.connect
def _bootstrap_pi_slot_pool(sender, **kwargs) -> None:
    """Fill the Redis-backed pi subprocess slot pool at worker startup.

    Runs once per worker process when it has finished bootstrapping and is
    connected to the broker. Top-up is idempotent: if the pool already has
    >= pi_max_concurrent tokens (e.g. from a previous run that didn't clean
    up), we leave it alone.
    """
    try:
        from app.utils.pi_slot import init_slots
        n = settings.pi_max_concurrent
        init_slots(n)
        logger.info("pi_slot_pool_ready", max_concurrent=n)
    except Exception as exc:
        # Don't crash the worker if Redis is briefly unavailable — the lazy
        # bootstrap in pi_slot() will retry on first use.
        logger.warning("pi_slot_pool_bootstrap_failed", error=str(exc))
