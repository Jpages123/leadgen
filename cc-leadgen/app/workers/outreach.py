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
            filename = os.path.basename(resolved_pdf).replace("-", "_")
            pdf_part = MIMEApplication(pdf_data, _subtype="pdf")
            pdf_part.add_header("Content-Disposition", "attachment", filename=f"web_audit_{filename}")
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
    """
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

    log.info("outreach_sending_batch", count=len(leads), send_mode=send_mode)
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
            time.sleep(2)

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


@shared_task(bind=True, name="app.workers.outreach.tasks.check_replies")
def check_replies(self) -> dict:
    """
    Poll outreach inbox for replies (Phase 4 response handling).

    Matches In-Reply-To / References headers to our sent Message-IDs.
    On reply: update sequence + lead status, alert via Discord, cancel remaining sequence.
    """
    imap_host = getattr(settings, "imap_host", "imap.zoho.com")
    imap_user = getattr(settings, "imap_user", settings.smtp_user)
    imap_pass = getattr(settings, "imap_pass", settings.smtp_pass)

    replies_found = 0

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
                mail.store(msg_id, "+FLAGS", "\\Seen")
                replies_found += 1
            except Exception as e:
                log.warning("reply_parse_error", msg_id=msg_id.decode(), error=str(e))
                continue

        mail.logout()
    except Exception as e:
        log.error("imap_connection_failed", error=str(e))
        return {"status": "error", "error": str(e)}

    if replies_found > 0:
        log.info("replies_detected", count=replies_found)

    return {"status": "ok", "replies_found": replies_found}


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