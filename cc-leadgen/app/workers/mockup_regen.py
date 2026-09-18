"""Celery task: retry mockup generation for leads stuck at mockup_status='failed'.

Two-gate flow (2026-07-10):
  Gate 1: Operator selects leads in /admin/mockup-lead-selection → queued
  Gate 2: Mockup builds → pending_approval → operator approves
  Failed:  Build error (transient) → retry via this task

This task handles the retry path. It picks up 'failed' leads, sets them back
to 'queued', and re-dispatches generate_mockup. Runs every 30 minutes via
Celery Beat.

The queue is button-driven — this task only handles transient build failures.
"""
from __future__ import annotations

from celery import shared_task

from app.config import get_settings
from app.db.sync_session import sync_session_scope
from app.models import Lead
from app.utils.logger import get_logger

log = get_logger(__name__)


@shared_task(bind=True, name="app.workers.mockup_regen.tasks.retry_failed_mockups")
def retry_failed_mockups(self, batch_size: int | None = None) -> dict:
    """Retry mockup generation for leads stuck at mockup_status='failed'.

    Args:
        batch_size: Max leads to retry per beat tick. Default 20.

    Returns:
        Dict with counts: {status, scanned, dispatched, errors}.
    """
    cfg = get_settings()
    if batch_size is None:
        batch_size = cfg.mockup_regen_batch_size
    threshold = cfg.mockup_pitch_score_threshold

    with sync_session_scope() as session:
        leads = (
            session.query(Lead)
            .filter(Lead.mockup_status == "failed")
            .filter(Lead.web_pitch_score.isnot(None))
            .filter(Lead.web_pitch_score >= threshold)
            .filter(Lead.website.isnot(None))
            .order_by(Lead.updated_at.asc())
            .limit(batch_size)
            .all()
        )

    if not leads:
        log.info("mockup_retry_queue_empty")
        return {"status": "ok", "scanned": 0, "dispatched": 0, "errors": 0}

    # Import lazily — the worker module is heavy (Playwright, Cloudflare,
    # Pi subprocess). Only pay the cost when there's actually work to do.
    from app.workers.mockup_generator import generate_mockup

    dispatched = 0
    errors = 0
    for lead in leads:
        lead_id = str(lead.id)
        try:
            # Move back to queued before dispatching so generate_mockup
            # guard passes (it allows 'queued' and 'failed')
            with sync_session_scope() as session:
                db_lead = session.get(Lead, lead.id)
                if db_lead and db_lead.mockup_status == "failed":
                    db_lead.mockup_status = "queued"
                    session.add(db_lead)
            generate_mockup.delay(lead_id)
            dispatched += 1
        except Exception as exc:
            errors += 1
            log.error(
                "mockup_retry_dispatch_error",
                lead_id=lead_id,
                error=str(exc),
            )

    log.info(
        "mockup_retry_done",
        scanned=len(leads),
        dispatched=dispatched,
        errors=errors,
        batch_size=batch_size,
    )
    return {
        "status": "ok",
        "scanned": len(leads),
        "dispatched": dispatched,
        "errors": errors,
    }


@shared_task(bind=True, name="app.workers.mockup_regen.tasks.retry_single_mockup")
def retry_single_mockup(self, lead_id: str) -> dict:
    """Retry mockup generation for a single failed lead.

    Schedules an immediate retry by re-dispatching generate_mockup.delay().
    Capped at mockup_max_auto_retries (default 3) — after that, the lead is
    left at mockup_status='failed' for the operator to handle manually
    (re-queue via /admin/mockup-lead-selection or manual SQL).

    Triggered by _mark_failed() in mockup_generator.py with a countdown
    of mockup_auto_retry_delay_s (default 60s) so transient issues
    (Cloudflare deploy blip, Pi session hiccup) have time to clear.

    Idempotent: re-running on a non-'failed' lead is a no-op. The 30-min
    retry_failed_mockups beat task still runs as a backstop in case this
    immediate retry path is bypassed (e.g. worker restart mid-cycle).
    """
    cfg = get_settings()
    max_retries = cfg.mockup_max_auto_retries

    with sync_session_scope() as session:
        lead = session.get(Lead, lead_id)
        if not lead:
            log.warning("retry_single_lead_not_found", lead_id=lead_id)
            return {"status": "skipped", "reason": "lead_not_found"}

        if lead.mockup_status != "failed":
            # Already picked up by something else (operator manual queue,
            # 30-min beat sweep, or a successful retry that bumped it
            # out of 'failed' before we got here).
            log.info(
                "retry_single_skipped_not_failed",
                lead_id=lead_id,
                current_status=lead.mockup_status,
            )
            return {"status": "skipped", "reason": "not_failed", "current_status": lead.mockup_status}

        current_count = lead.mockup_retry_count or 0
        if current_count >= max_retries:
            log.warning(
                "retry_single_max_exceeded",
                lead_id=lead_id,
                retry_count=current_count,
                max=max_retries,
            )
            return {
                "status": "skipped",
                "reason": "max_retries_exceeded",
                "retry_count": current_count,
                "max": max_retries,
            }

        lead.mockup_status = "queued"
        lead.mockup_retry_count = current_count + 1
        session.add(lead)

    # Dispatch outside the session so the task can take its time.
    from app.workers.mockup_generator import generate_mockup
    generate_mockup.delay(lead_id)

    log.info(
        "retry_single_dispatched",
        lead_id=lead_id,
        retry_count=current_count + 1,
        max=max_retries,
    )
    return {
        "status": "dispatched",
        "lead_id": lead_id,
        "retry_count": current_count + 1,
        "max": max_retries,
    }
