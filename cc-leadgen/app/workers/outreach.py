"""
Outreach worker — email sequences via SMTP, reply handling via IMAP.

Phase 3 (Email) + Phase 4 (Response Handling) combined.
SMTP: Zoho (outreach@clientcompass.co.za)

Safety gates (send_mode = 'test' defaults on):
  - SQL query filter: only whitelisted emails selected from DB
  - SMTP-level defence in depth: any non-whitelisted email skipped at send time
  Flip to live mode by setting SEND_MODE=live in .env
"""
from __future__ import annotations

import hashlib
import imaplib
import smtplib
import time
import uuid
from datetime import datetime, timedelta, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.application import MIMEApplication
from email.mime.text import MIMEText
from typing import Optional

from celery import shared_task
from sqlalchemy import func, select

from app.config import get_settings
from app.db.sync_session import sync_session_scope
from app.models import Lead, LeadEvent, OutreachSequence, OutreachTemplate
from app.utils.logger import get_logger
from app.utils.email_builder import render_email_for_lead


def _slugify_for_attachment(name: str) -> str:
    """Business name → attachment-safe slug.

    Strips non-alphanumeric chars, collapses runs, keeps Title Case.
    Used purely for the MIME attachment filename; the file on disk
    keeps its original <slug>.pdf name in <project>/.cache/reports/.
    """
    import re as _re
    s = _re.sub(r"[^A-Za-z0-9]+", "-", (name or "")).strip("-")
    return s[:80] or "lead"

log = get_logger(__name__)
settings = get_settings()

# ── SMTP Client ────────────────────────────────────────────────────────────────

def _make_smtp() -> smtplib.SMTP:
    """Create authenticated SMTP connection to Zoho."""
    smtp = smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=20)
    smtp.starttls()
    smtp.login(settings.smtp_user, settings.smtp_pass)
    return smtp


def _build_email(from_email: str, to_email: str, subject: str,
                html_body: str, text_body: str, in_reply_to: str = None
                ) -> MIMEMultipart:
    """Build a MIME email with tracking pixel."""
    msg = MIMEMultipart("alternative")
    msg["From"] = f"{settings.smtp_from_name} <{from_email}>"
    msg["To"] = to_email
    msg["Subject"] = subject
    msg["Message-ID"] = f"<{uuid.uuid4().hex}@outreach.clientcompass.co.za>"

    if in_reply_to:
        msg["In-Reply-To"] = in_reply_to
        msg["References"] = in_reply_to

    msg.attach(MIMEText(text_body, "plain", "utf-8"))
    tracking_pixel = (
        f'<img src="https://cc-leadgen.clientcompass.co.za/track/'
        f'{hashlib.md5(to_email.encode()).hexdigest()}.png" width="1" height="1" />'
    )
    html_with_tracking = html_body.replace("</body>", tracking_pixel + "</body>")
    msg.attach(MIMEText(html_with_tracking, "html", "utf-8"))
    return msg



def _build_email_with_pdf(
    from_email: str,
    to_email: str,
    subject: str,
    html_body: str,
    text_body: str,
    pdf_path: str | None = None,
    in_reply_to: str = None,
    lead=None,
) -> MIMEMultipart:
    """Build a MIME email with optional PDF attachment.

    The ``pdf_path`` argument is the DB-stored path (often a stale
    ``/tmp/cc_reports/<slug>.pdf`` that may no longer exist on disk).
    Resolution and on-demand regeneration is delegated to
    ``report_assets.resolve`` / ``regenerate_pdf_for_lead`` (Session 15
    fix). Pass ``lead`` to enable on-demand regen when the file is
    missing; pass ``None`` to fall back to the legacy "log and skip"
    behaviour.
    """
    # Resolve the PDF through the durable-first lookup. Same chain as
    # email_draft.send_email_draft: durable → legacy → on-demand regen.
    from pathlib import Path as _Path
    resolved_pdf: "_Path | None" = None
    if pdf_path:
        try:
            from app.utils.report_assets import resolve as _resolve_report
            stem = _Path(pdf_path).stem
            resolved_pdf = _resolve_report(stem)
        except Exception as exc:
            log.warning("outreach_pdf_resolve_failed", path=pdf_path, error=str(exc))

    if resolved_pdf is None and lead is not None:
        try:
            from app.utils.report_assets import regenerate_pdf_for_lead
            regenerated = regenerate_pdf_for_lead(lead)
            if regenerated:
                resolved_pdf = _Path(regenerated)
                log.info("outreach_pdf_regen_inline", path=regenerated)
        except Exception as exc:
            log.warning("outreach_pdf_regen_failed", error=str(exc))

    if resolved_pdf and resolved_pdf.exists() and resolved_pdf.stat().st_size > 0:
        outer = MIMEMultipart("mixed")
        outer["From"] = f"{settings.smtp_from_name} <{from_email}>"
        outer["To"] = to_email
        outer["Subject"] = subject
        outer["Message-ID"] = f"<{uuid.uuid4().hex}@outreach.clientcompass.co.za>"
        if in_reply_to:
            outer["In-Reply-To"] = in_reply_to
            outer["References"] = in_reply_to

        # Alternative part (text + html)
        alt = MIMEMultipart("alternative")
        alt.attach(MIMEText(text_body, "plain", "utf-8"))
        tracking_pixel = (
            f'<img src="https://cc-leadgen.clientcompass.co.za/track/'
            f'{hashlib.md5(to_email.encode()).hexdigest()}.png" width="1" height="1" />'
        )
        html_with_tracking = html_body.replace("</body>", tracking_pixel + "</body>")
        alt.attach(MIMEText(html_with_tracking, "html", "utf-8"))
        outer.attach(alt)

        # PDF attachment
        try:
            import os
            with open(resolved_pdf, "rb") as f:
                pdf_data = f.read()
            _display_name = getattr(lead, "business_name", None) if lead is not None else None
            if not _display_name:
                _display_name = os.path.basename(resolved_pdf).replace(".pdf", "").replace("_", " ").replace("-", " ").title()
            _attachment = "Your-Website-Audit-" + _slugify_for_attachment(_display_name) + ".pdf"
            pdf_part = MIMEApplication(pdf_data, _subtype="pdf")
            pdf_part.add_header("Content-Disposition", "attachment", filename=_attachment)
            outer.attach(pdf_part)
        except Exception as exc:
            log.warning("pdf_attach_failed", path=str(resolved_pdf), error=str(exc))

        return outer
    elif pdf_path:
        # Original path was set but no PDF could be sourced — fall back to
        # the plain email without attachment, but log clearly so operators
        # can spot the pattern if it recurs.
        log.warning("outreach_pdf_skipped", reason="missing_or_empty", original_path=pdf_path)

    return _build_email(from_email, to_email, subject, html_body, text_body, in_reply_to)


# ── Outreach Worker ────────────────────────────────────────────────────────────

@shared_task(bind=True, name="app.workers.outreach.tasks.send_email_sequence")
def send_email_sequence(self, batch_size: int = 10) -> dict:
    """
    Pick up outreach_queued leads, create email sequences, and send first touch.

    Rate limiting:
    - Max EMAIL_DAILY_LIMIT emails per day
    - Min EMAIL_MIN_GAP_HOURS between touches to same lead
    - At most batch_size emails per Celery run

    Safety gates (send_mode = 'test' defaults on):
    - SQL query filter: only whitelisted emails selected from DB
    - SMTP-level defence in depth: any non-whitelisted email skipped at send time
    Flip to live by setting SEND_MODE=live in .env

    Business-hours gating: when outreach_business_hours_only is enabled
    (default), refuses to send outside Mon-Fri [start_hour, end_hour) in the
    configured timezone — same guard as send_follow_up_sequence (Phase N).
    Added after a real incident where the outreach-queue-leads (every 2h)
    and outreach-send (every 30 min) beat ticks landed together at 00:00 UTC
    and cold-emailed 5 real prospects at 02:00 SAST. Candidates remain
    eligible and simply fire on the next beat tick inside business hours.

    Kill switch (settings.outreach_send_enabled, default False, 2026-08-28):
    this is the only outreach path with zero operator review — it selects
    `outreach_queued` leads with no PDF/approved mockup and sends them the
    old `default`/`hair_beauty`/`cleaning` WhatsApp-pitch templates straight
    over SMTP. After the pivot to web-revamp-only, that pitch is no longer
    accurate for any live lead, and a ~130-lead pre-pivot backlog was
    silently drip-feeding those emails out (e.g. Kodec Auto Electrical,
    Felicia's Carpet Cleaning, Carma Cleaning, Peter Bretherton Landscapes —
    2026-08-27/28) with no draft/approval step at all. The `outreach-send`
    beat entry has also been removed in `celery_app.py`; this flag is
    belt-and-suspenders in case the task is ever manually re-triggered or
    re-scheduled. Flip back on only once a reviewed draft flow exists for
    generic (non-web-revamp) leads.
    """
    if not getattr(settings, "outreach_send_enabled", False):
        log.warning("outreach_send_disabled", reason="pivoted_to_web_revamp_only")
        return {"status": "ok", "sent": 0, "reason": "disabled_post_pivot"}

    if getattr(settings, "outreach_business_hours_only", True) and not is_business_time():
        local = _local_now()
        log.info(
            "outreach_outside_business_hours",
            local_iso=local.isoformat(),
            weekday=local.weekday(),
            hour=local.hour,
        )
        return {"status": "ok", "sent": 0, "reason": "outside_business_hours"}

    daily_limit = settings.email_daily_limit or 5
    min_gap_hours = settings.email_min_gap_hours or 72
    send_mode = settings.send_mode or "test"
    whitelist = {e.lower() for e in settings.test_email_whitelist}

    log.info("outreach_run_started", send_mode=send_mode)

    with sync_session_scope() as session:
        today_start = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
        sent_today = session.execute(
            select(func.count(OutreachSequence.id)).where(
                OutreachSequence.status == "sent",
                OutreachSequence.sent_at >= today_start,
            )
        ).scalar() or 0

        remaining = max(0, daily_limit - sent_today)
        to_send = min(remaining, batch_size)

        if to_send == 0:
            log.info("outreach_daily_cap_reached", sent_today=sent_today, limit=daily_limit)
            return {"status": "ok", "sent": 0, "reason": "daily_cap_reached"}

        min_gap = datetime.now(timezone.utc) - timedelta(hours=min_gap_hours)
        query = (
            select(Lead).where(
                Lead.status == "outreach_queued",
                Lead.mockup_status.in_(["none", "rejected", "failed"]),  # skip approved + pending_approval (draft flow handles those)
                Lead.email.isnot(None),
                (Lead.last_contacted_at.is_(None)) | (Lead.last_contacted_at < min_gap),
                ~Lead.id.in_(
                    select(OutreachSequence.lead_id).where(
                        OutreachSequence.status.in_(["pending", "sent"]),
                    )
                ),
            )
        )

        if send_mode == "test":
            # SQL-level gate: only whitelisted test addresses
            query = query.where(Lead.email.in_(settings.test_email_whitelist))
            log.info("outreach_test_mode_active", whitelist=settings.test_email_whitelist)

        leads = session.execute(query.limit(to_send)).scalars().all()

    if not leads:
        log.info("outreach_no_leads_ready", send_mode=send_mode)
        return {"status": "ok", "sent": 0, "reason": "no_leads_ready"}

    # Within-day pacing: spread step-1 sends across today's business hours
    # instead of firing a freshly-queued batch as one burst. See
    # compute_pacing_seconds (shared with send_follow_up_sequence).
    if getattr(settings, "outreach_pace_across_business_hours", True):
        pacing_seconds = compute_pacing_seconds(len(leads))
    else:
        pacing_seconds = 0.0

    log.info("outreach_sending_batch", count=len(leads), send_mode=send_mode, pacing_seconds=pacing_seconds)
    sent = failed = 0

    try:
        smtp = _make_smtp()
    except Exception as e:
        log.error("smtp_connection_failed", error=str(e))
        return {"status": "error", "error": f"smtp_failed: {e}"}

    for lead in leads:
        if not lead.email:
            continue

        # SMTP-level defence in depth
        if send_mode == "test" and lead.email.lower() not in whitelist:
            log.warning("smtp_test_mode_blocked", email=lead.email, lead_id=str(lead.id))
            continue

        try:
            content = render_email_for_lead(lead)
            subject, text_body, html_body = content.subject, content.body_text, content.body_html
            template_key = content.template_key

            msg = _build_email_with_pdf(
                from_email=settings.smtp_from_email,
                to_email=lead.email,
                subject=subject,
                html_body=html_body,
                text_body=text_body,
                pdf_path=getattr(lead, "web_audit_pdf_path", None),
                lead=lead,  # Session 15 fix — enables on-demand PDF regen
            )

            smtp.sendmail(settings.smtp_from_email, [lead.email], msg.as_string())

            with sync_session_scope() as session:
                lead_db = session.get(Lead, lead.id)
                if lead_db:
                    seq = OutreachSequence(
                        lead_id=lead.id,
                        channel="email",
                        sequence_name=f"{template_key}_v1",
                        step_number=1,
                        subject=subject,
                        message_body=text_body,
                        status="sent",
                        sent_at=datetime.now(timezone.utc),
                        message_id=msg["Message-ID"],
                    )
                    lead_db.last_contacted_at = datetime.now(timezone.utc)
                    lead_db.status = "contacted"
                    session.add(seq)
                    session.add(lead_db)
                    event = LeadEvent(
                        lead_id=lead.id,
                        event_type="email_sent",
                        payload={"subject": subject, "step": 1, "template": template_key, "pdf_attached": bool(getattr(lead, "web_audit_pdf_path", None))},
                    )
                    session.add(event)

            sent += 1
            log.info("outreach_email_sent", lead_id=str(lead.id), email=lead.email, send_mode=send_mode)
            time.sleep(pacing_seconds or 2)

        except Exception as e:
            log.error("outreach_send_failed", lead_id=str(lead.id), error=str(e))
            failed += 1
            continue

    try:
        smtp.quit()
    except Exception:
        pass

    log.info("outreach_batch_done", sent=sent, failed=failed, send_mode=send_mode)
    return {"status": "ok", "sent": sent, "failed": failed}


def _handle_reply(candidate_message_ids: set[str], from_email: str | None, subject: str) -> bool:
    """Match an inbound message's headers against a sent OutreachSequence.

    On match: marks that sequence 'replied', skips any other still-pending
    sequence rows for the lead (stops further follow-ups), flips the lead
    to 'responded', clears next_follow_up_at, logs a LeadEvent, and fires a
    Discord alert. Returns True if a matching sequence was found.
    """
    if not candidate_message_ids:
        return False

    with sync_session_scope() as session:
        seq = session.execute(
            select(OutreachSequence).where(
                OutreachSequence.message_id.in_(candidate_message_ids),
                OutreachSequence.status == "sent",
            )
        ).scalars().first()
        if not seq:
            return False

        lead = session.get(Lead, seq.lead_id)
        if not lead:
            return False

        seq.status = "replied"
        session.add(seq)

        # Stop any other sequence rows still awaiting send for this lead.
        others = session.execute(
            select(OutreachSequence).where(
                OutreachSequence.lead_id == lead.id,
                OutreachSequence.id != seq.id,
                OutreachSequence.status == "pending",
            )
        ).scalars().all()
        for other in others:
            other.status = "skipped"
            session.add(other)

        lead.status = "responded"
        lead.next_follow_up_at = None
        session.add(lead)

        session.add(LeadEvent(
            lead_id=lead.id,
            event_type="replied",
            payload={"step": seq.step_number, "from_email": from_email, "subject": subject},
        ))

        step_number = seq.step_number
        business_name = lead.business_name
        lead_id = str(lead.id)

    log.info("reply_matched", lead_id=lead_id, step=step_number)

    if settings.discord_alert_on_reply:
        try:
            from app.utils.discord import send_alert_sync
            send_alert_sync(
                content=(
                    f"📩 **Reply received** — {business_name} replied "
                    f"(step {step_number}). Subject: {subject or '(no subject)'}"
                )
            )
        except Exception as exc:
            log.warning("discord_alert_failed", error=str(exc))

    return True


@shared_task(bind=True, name="app.workers.outreach.tasks.check_replies")
def check_replies(self) -> dict:
    """
    Poll outreach inbox for replies (Phase 4 response handling).

    Matches In-Reply-To / References headers to our sent Message-IDs.
    On reply: update sequence + lead status, alert via Discord, cancel remaining sequence.
    """
    import email as email_lib
    from email.utils import getaddresses

    imap_host = getattr(settings, "imap_host", "imap.zoho.com")
    imap_user = getattr(settings, "imap_user", settings.smtp_user)
    imap_pass = getattr(settings, "imap_pass", settings.smtp_pass)

    replies_found = 0
    matched = 0

    try:
        mail = imaplib.IMAP4_SSL(imap_host)
        mail.login(imap_user, imap_pass)
        mail.select("INBOX")

        status, msg_ids = mail.search(None, "UNSEEN")
        if status != "OK":
            return {"status": "error", "reason": "imap_search_failed"}

        for msg_id in msg_ids[0].split():
            try:
                status, msg_data = mail.fetch(msg_id, "(RFC822)")
                if status != "OK":
                    continue
                raw = msg_data[0][1]
                msg = email_lib.message_from_bytes(raw)
                mail.store(msg_id, "+FLAGS", "\\Seen")
                replies_found += 1

                candidate_ids: set[str] = set()
                in_reply_to = (msg.get("In-Reply-To") or "").strip()
                if in_reply_to:
                    candidate_ids.add(in_reply_to)
                references = (msg.get("References") or "").strip()
                if references:
                    candidate_ids.update(references.split())

                from_addrs = getaddresses(msg.get_all("From", []))
                from_email = from_addrs[0][1] if from_addrs else None
                subject = msg.get("Subject", "") or ""

                if _handle_reply(candidate_ids, from_email, subject):
                    matched += 1
            except Exception as e:
                log.warning("reply_parse_error", msg_id=msg_id.decode(), error=str(e))
                continue

        mail.logout()
    except Exception as e:
        log.error("imap_connection_failed", error=str(e))
        return {"status": "error", "error": str(e)}

    if replies_found > 0:
        log.info("replies_detected", count=replies_found, matched=matched)

    return {"status": "ok", "replies_found": replies_found, "matched": matched}


@shared_task(bind=True, name="app.workers.outreach.tasks.queue_leads_for_outreach")
def queue_leads_for_outreach(self, min_score: int = 40) -> dict:
    """
    Move enriched leads above min_score into outreach_queued.
    Run daily as part of the scoring pipeline.
    """
    with sync_session_scope() as session:
        updated = session.execute(
            select(Lead).where(
                Lead.status == "enriched",
                Lead.score >= min_score,
                Lead.email.isnot(None),
                Lead.mockup_status.in_(["none", "rejected", "failed"]),  # mockup-approved leads use draft flow
            )
        ).scalars().all()

        count = 0
        for lead in updated:
            lead.status = "outreach_queued"
            session.add(lead)
            count += 1

        log.info("outreach_queue_updated", count=count)
        return {"status": "ok", "queued": count}


# ── Follow-up Sequences ─────────────────────────────────────────────────────
# Web-revamp leads only (mockup_status='approved'). Auto-sends (no operator
# review) — same safety gates as send_email_sequence.
#   Step 2 ~settings.follow_up_step2_gap_hours after step 1 sent
#   Step 3 ~settings.follow_up_step3_gap_hours after step 2 sent, then the
#   lead is marked 'no_response' and no further touches are sent.
# A reply at any point (see check_replies) flips the lead to 'responded'
# and removes it from these queries before its next touch is due.

def _local_now() -> datetime:
    """Return current wall-clock time in the configured business timezone (default SAST)."""
    offset = timedelta(hours=getattr(settings, "follow_up_business_tz_offset_hours", 2))
    return datetime.now(timezone.utc).astimezone(timezone(offset))


def is_business_time(now: datetime | None = None) -> bool:
    """True iff `now` (default: current time in configured TZ) falls on Mon-Fri [start, end)."""
    if now is None:
        now = _local_now()
    start = getattr(settings, "follow_up_business_start_hour", 8)
    end = getattr(settings, "follow_up_business_end_hour", 17)
    if now.weekday() >= 5:  # Sat=5, Sun=6
        return False
    return start <= now.hour < end


def _claim_lead(session, lead_id, ttl_seconds: int = 300) -> bool:
    """Atomically claim `lead_id` for the next `ttl_seconds`. Returns True if
    this caller now owns the claim, False if another worker holds it.

    Uses a single UPDATE with RETURNING — no separate SELECT needed. The TTL
    is a safety net for crashed workers; a normally-completing send lets the
    claim expire naturally without an explicit release (avoids a second
    round-trip and a race between "send succeeded" and "release claim")."""
    from sqlalchemy import text as _sql_text
    row = session.execute(
        _sql_text(
            "UPDATE leads "
            "SET claimed_until = NOW() + (:ttl || ' seconds')::interval "
            "WHERE id = :lid "
            "  AND (claimed_until IS NULL OR claimed_until < NOW()) "
            "RETURNING id"
        ),
        {"lid": lead_id, "ttl": ttl_seconds},
    ).first()
    return row is not None


def compute_pacing_seconds(count: int) -> float:
    """Return the number of seconds to sleep between sends for `count` candidates,
    paced across today's business hours. With the default 08:00–17:00 window
    (9 hours = 32400s):
      count=15 → 2160s = 36 min between sends
      count=10 → 3240s = 54 min
      count=5  → 6480s = 1h48m
      count=3  → 10800s = 3h
      count=1  → 0s   (send immediately)
    Result is clamped to follow_up_pace_min_interval_seconds."""
    if count <= 1:
        return 0.0
    start = getattr(settings, "follow_up_business_start_hour", 8)
    end = getattr(settings, "follow_up_business_end_hour", 17)
    business_window_seconds = max(1, (end - start) * 3600)
    raw = business_window_seconds / count
    floor = max(0, getattr(settings, "follow_up_pace_min_interval_seconds", 30))
    return max(raw, floor)


@shared_task(bind=True, name="app.workers.outreach.tasks.send_follow_up_sequence")
def send_follow_up_sequence(self, batch_size: int = 10) -> dict:
    """Send step-2/step-3 follow-ups for web-revamp leads that haven't replied.

    Hardened in Phase M (2026-08): when follow_up_business_hours_only is enabled
    (default), refuse to send outside Mon-Fri [start_hour, end_hour) in the
    configured timezone. Eligible candidates stay eligible — the next beat tick
    that lands in business hours will fire them. No state mutation on skip.
    """
    if getattr(settings, "follow_up_business_hours_only", True) and not is_business_time():
        local = _local_now()
        log.info(
            "followup_outside_business_hours",
            local_iso=local.isoformat(),
            weekday=local.weekday(),
            hour=local.hour,
        )
        return {"status": "ok", "sent": 0, "reason": "outside_business_hours"}

    daily_limit = settings.email_daily_limit or 5
    send_mode = settings.send_mode or "test"
    whitelist = {e.lower() for e in settings.test_email_whitelist}
    step2_gap = timedelta(hours=settings.follow_up_step2_gap_hours or 96)
    step3_gap = timedelta(hours=settings.follow_up_step3_gap_hours or 120)
    now = datetime.now(timezone.utc)

    with sync_session_scope() as session:
        today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        sent_today = session.execute(
            select(func.count(OutreachSequence.id)).where(
                OutreachSequence.status == "sent",
                OutreachSequence.sent_at >= today_start,
            )
        ).scalar() or 0
        remaining = max(0, daily_limit - sent_today)
        if remaining == 0:
            log.info("followup_daily_cap_reached", sent_today=sent_today, limit=daily_limit)
            return {"status": "ok", "sent": 0, "reason": "daily_cap_reached"}

        candidates: list[tuple] = []  # (lead_id, step_number, prior_message_id)

        def _blocked(lead_id: uuid.UUID, next_steps: list[int]) -> bool:
            return session.execute(
                select(OutreachSequence.id).where(
                    OutreachSequence.lead_id == lead_id,
                    (OutreachSequence.status == "replied")
                    | (OutreachSequence.step_number.in_(next_steps)),
                )
            ).first() is not None

        def _latest_per_lead(rows: list[OutreachSequence]) -> list[OutreachSequence]:
            """Collapse to (at most) one row per lead — the most recent send.

            A lead can end up with more than one 'sent' row at a given step
            (e.g. a dev/test lead re-sent manually several times before this
            phase existed). Without this, each row would independently pass
            the eligibility check below and generate a duplicate candidate
            for the same lead in the same batch, since _blocked() only sees
            DB state as of the top of this function — it can't see sends
            that happen later in this same run.
            """
            latest: dict[uuid.UUID, OutreachSequence] = {}
            for row in rows:
                current = latest.get(row.lead_id)
                if current is None or (row.sent_at or now) > (current.sent_at or now):
                    latest[row.lead_id] = row
            return list(latest.values())

        seen_lead_ids: set[uuid.UUID] = set()

        step1_rows = _latest_per_lead(session.execute(
            select(OutreachSequence).where(
                OutreachSequence.step_number == 1,
                OutreachSequence.status == "sent",
                OutreachSequence.sent_at <= now - step2_gap,
            )
        ).scalars().all())
        for seq in step1_rows:
            lead = session.get(Lead, seq.lead_id)
            if not lead or lead.status != "contacted" or lead.mockup_status != "approved" or not lead.email:
                continue
            if lead.id in seen_lead_ids or _blocked(lead.id, [2, 3]):
                continue
            seen_lead_ids.add(lead.id)
            candidates.append((lead.id, 2, seq.message_id))

        step2_rows = _latest_per_lead(session.execute(
            select(OutreachSequence).where(
                OutreachSequence.step_number == 2,
                OutreachSequence.status == "sent",
                OutreachSequence.sent_at <= now - step3_gap,
            )
        ).scalars().all())
        for seq in step2_rows:
            lead = session.get(Lead, seq.lead_id)
            if not lead or lead.status != "contacted" or lead.mockup_status != "approved" or not lead.email:
                continue
            if lead.id in seen_lead_ids or _blocked(lead.id, [3]):
                continue
            seen_lead_ids.add(lead.id)
            candidates.append((lead.id, 3, seq.message_id))

        candidates = candidates[: min(remaining, batch_size)]
        # Within-day pacing: spread sends across today's business hours
        # so a backlog doesn't fire as one burst. See compute_pacing_seconds.
        if getattr(settings, "follow_up_pace_across_business_hours", True):
            pacing_seconds = compute_pacing_seconds(len(candidates))
        else:
            pacing_seconds = 0.0
        lead_ids = [c[0] for c in candidates]
        leads_by_id = (
            {l.id: l for l in session.execute(select(Lead).where(Lead.id.in_(lead_ids))).scalars().all()}
            if lead_ids else {}
        )

    if not candidates:
        log.info("followup_no_leads_ready", send_mode=send_mode)
        return {"status": "ok", "sent": 0, "reason": "no_leads_ready"}

    log.info(
        "followup_sending_batch",
        count=len(candidates),
        send_mode=send_mode,
        pacing_seconds=pacing_seconds,
    )
    sent = failed = 0

    try:
        smtp = _make_smtp()
    except Exception as e:
        log.error("smtp_connection_failed", error=str(e))
        return {"status": "error", "error": f"smtp_failed: {e}"}

    already_sent_lead_ids: set = set()  # belt-and-suspenders — see _latest_per_lead above
    for lead_id, step_number, prior_message_id in candidates:
        if lead_id in already_sent_lead_ids:
            continue
        lead = leads_by_id.get(lead_id)
        if not lead or not lead.email:
            continue

        if send_mode == "test" and lead.email.lower() not in whitelist:
            log.warning("smtp_test_mode_blocked", email=lead.email, lead_id=str(lead.id))
            continue

        # Atomic cross-worker claim — only one worker can hold this lead for the
        # next ttl_seconds. Prevents two concurrent beat ticks from collapsing
        # the within-day pacing to ~0. Migration 012 added the column.
        with sync_session_scope() as claim_session:
            if not _claim_lead(claim_session, lead_id):
                log.info("followup_skip_already_claimed", lead_id=str(lead_id), step=step_number)
                continue

        try:
            content = render_email_for_lead(lead, step=step_number)

            msg = _build_email(
                from_email=settings.smtp_from_email,
                to_email=lead.email,
                subject=content.subject,
                html_body=content.body_html,
                text_body=content.body_text,
                in_reply_to=prior_message_id,
            )

            smtp.sendmail(settings.smtp_from_email, [lead.email], msg.as_string())

            with sync_session_scope() as session:
                lead_db = session.get(Lead, lead.id)
                if lead_db:
                    seq = OutreachSequence(
                        lead_id=lead.id,
                        channel="email",
                        sequence_name=f"{content.template_key}_v1",
                        step_number=step_number,
                        subject=content.subject,
                        message_body=content.body_text,
                        status="sent",
                        sent_at=datetime.now(timezone.utc),
                        message_id=msg["Message-ID"],
                    )
                    lead_db.last_contacted_at = datetime.now(timezone.utc)
                    if step_number >= 3:
                        lead_db.status = "no_response"
                        lead_db.next_follow_up_at = None
                    else:
                        lead_db.next_follow_up_at = datetime.now(timezone.utc) + step3_gap
                    session.add(lead_db)
                    session.add(seq)
                    session.add(LeadEvent(
                        lead_id=lead.id,
                        # Distinct from send_email_sequence's "email_sent" so the
                        # admin activity feed can render the follow-up icon
                        # (EVENT_LABELS['follow_up_sent']) instead of a generic send.
                        event_type="follow_up_sent",
                        payload={"step": step_number, "template": content.template_key},
                    ))

            already_sent_lead_ids.add(lead_id)
            sent += 1
            log.info("followup_email_sent", lead_id=str(lead.id), step=step_number, send_mode=send_mode)
            # Paced sleep — see compute_pacing_seconds(). Falls back to 2s when
            # pacing is disabled so we still respect SMTP connection etiquette.
            time.sleep(pacing_seconds or 2)

        except Exception as e:
            log.error("followup_send_failed", lead_id=str(lead.id), step=step_number, error=str(e))
            failed += 1
            continue

    try:
        smtp.quit()
    except Exception:
        pass

    log.info("followup_batch_done", sent=sent, failed=failed, send_mode=send_mode)
    return {"status": "ok", "sent": sent, "failed": failed}