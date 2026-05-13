"""
Outreach worker — email sequences via SMTP, reply handling via IMAP.

Phase 3 (Email) + Phase 4 (Response Handling) combined.
SMTP: Zoho (outreach@clientcompass.co.za)
"""
from __future__ import annotations

import hashlib
import imaplib
import smtplib
import time
import uuid
from datetime import datetime, timedelta, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.header import decode_header
from typing import Optional

from celery import shared_task
from jinja2 import Template
from sqlalchemy import and_, func, select

from app.config import get_settings
from app.db.sync_session import sync_session_scope
from app.models import Lead, LeadEvent, OutreachSequence, OutreachTemplate
from app.utils.logger import get_logger

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

    # Plain text
    msg.attach(MIMEText(text_body, "plain", "utf-8"))
    # HTML with tracking pixel
    tracking_pixel = (
        f'<img src="https://cc-leadgen.clientcompass.co.za/track/'
        f'{hashlib.md5(to_email.encode()).hexdigest()}.png" width="1" height="1" />'
    )
    html_with_tracking = html_body.replace("</body>", tracking_pixel + "</body>")
    msg.attach(MIMEText(html_with_tracking, "html", "utf-8"))

    return msg


# ── Template Rendering ────────────────────────────────────────────────────────

EMAIL_TEMPLATES = {
    "hair_beauty": """
Hi {{ owner_name or 'there' }},

Saw {{ business_name }} on Yep Mall — looks like you're doing great work.

Quick question: do clients book or message you through WhatsApp? If so, how are you managing it when you're with a client?

Most hair and beauty salons I talk to say WhatsApp is their busiest channel — but also the hardest to keep up with.

We built a tool specifically for that — I can show you how in 10 minutes if you're curious.

Reply YES to get a free walkthrough on WhatsApp, or just reply with any questions.

Talk soon,
{{ sender_name }}
Client Compass
""",
    "cleaning": """
Hi {{ owner_name or 'there' }},

Found {{ business_name }} on Yep Mall — great to see you offering cleaning services.

Quick question: how do you handle enquiries when you're already with a client? Do clients WhatsApp you, and if so — does it get overwhelming?

Most solo cleaners I speak to say WhatsApp is their #1 channel for getting bookings, but it's also where they lose leads because they can't reply fast enough.

We built a tool for that — it keeps your WhatsApp organized even when you can't check it.

Interested in seeing how it works? Reply YES for a free 10-minute walkthrough on WhatsApp.

Or just reply with any questions — happy to help either way.

{{ sender_name }}
Client Compass
""",
    "default": """
Hi {{ owner_name or 'there' }},

I came across {{ business_name }} and wanted to reach out.

Do you use WhatsApp for communicating with clients or taking bookings? If so, how do you manage it when you're busy?

We built a simple tool that helps businesses like yours keep WhatsApp organized — so you never miss a enquiry even when you're hands-full.

Reply YES if you'd like to see a quick demo on WhatsApp.

{{ sender_name }}
Client Compass
""",
}

HTML_TEMPLATES = {
    "hair_beauty": """
<!DOCTYPE html>
<html><body style="font-family: Arial, sans-serif; max-width: 600px; margin: auto; padding: 20px;">
<p>Hi {{ owner_name or 'there' }},</p>
<p>Saw <strong>{{ business_name }}</strong> on Yep Mall — looks like you're doing great work.</p>
<p>Quick question: do clients book or message you through WhatsApp? If so, how are you managing it when you're with a client?</p>
<p>Most hair and beauty salons I talk to say WhatsApp is their busiest channel — but also the hardest to keep up with.</p>
<p>We built a tool specifically for that. I can show you how in 10 minutes if you're curious.</p>
<p><a href="https://clientcompass.co.za" style="background:#3b82f6;color:white;padding:10px 20px;text-decoration:none;border-radius:5px;">Reply YES for a free WhatsApp demo</a></p>
<p>Or just reply with any questions — happy to help.</p>
<p>Talk soon,<br>{{ sender_name }}<br>Client Compass<br><a href="https://clientcompass.co.za">clientcompass.co.za</a></p>
<hr style="border:none;border-top:1px solid #eee;margin:20px 0;">
<p style="font-size:12px;color:#888;">You're receiving this because you run a business in South Africa and we think Client Compass could help. 
<a href="{{ unsubscribe_url }}">Unsubscribe</a> — we respect your time.</p>
</body></html>
""",
    "cleaning": """
<!DOCTYPE html>
<html><body style="font-family: Arial, sans-serif; max-width: 600px; margin: auto; padding: 20px;">
<p>Hi {{ owner_name or 'there' }},</p>
<p>Found <strong>{{ business_name }}</strong> on Yep Mall — great to see you offering cleaning services.</p>
<p>Quick question: how do you handle enquiries when you're already with a client? Do clients WhatsApp you, and if so — does it get overwhelming?</p>
<p>Most solo cleaners I speak to say WhatsApp is their #1 channel for getting bookings, but it's also where they lose leads.</p>
<p>We built a tool for that. Interested in seeing how it works?</p>
<p><a href="https://clientcompass.co.za" style="background:#3b82f6;color:white;padding:10px 20px;text-decoration:none;border-radius:5px;">Reply YES for a free WhatsApp demo</a></p>
<p>{{ sender_name }}<br>Client Compass<br><a href="https://clientcompass.co.za">clientcompass.co.za</a></p>
<hr style="border:none;border-top:1px solid #eee;margin:20px 0;">
<p style="font-size:12px;color:#888;">You're receiving this because you run a business in South Africa. 
<a href="{{ unsubscribe_url }}">Unsubscribe</a> — we respect your time.</p>
</body></html>
""",
    "default": """
<!DOCTYPE html>
<html><body style="font-family: Arial, sans-serif; max-width: 600px; margin: auto; padding: 20px;">
<p>Hi {{ owner_name or 'there' }},</p>
<p>I came across <strong>{{ business_name }}</strong> and wanted to reach out.</p>
<p>Do you use WhatsApp for communicating with clients or taking bookings? If so, how do you manage it when you're busy?</p>
<p>We built a simple tool that helps businesses keep WhatsApp organized.</p>
<p><a href="https://clientcompass.co.za" style="background:#3b82f6;color:white;padding:10px 20px;text-decoration:none;border-radius:5px;">Reply YES for a free demo</a></p>
<p>{{ sender_name }}<br>Client Compass<br><a href="https://clientcompass.co.za">clientcompass.co.za</a></p>
<hr style="border:none;border-top:1px solid #eee;margin:20px 0;">
<p style="font-size:12px;color:#888;"><a href="{{ unsubscribe_url }}">Unsubscribe</a></p>
</body></html>
""",
}

SENDER_NAME = "Mario from Client Compass"

def _get_template(lead: Lead) -> tuple[str, str, str]:
    """Pick the right template for a lead based on category."""
    category = (lead.business_type or "").lower()
    if any(k in category for k in ["hair", "beauty", "nail", "barber", "salon"]):
        key = "hair_beauty"
    elif any(k in category for k in ["clean", "maid"]):
        key = "cleaning"
    else:
        key = "default"
    return EMAIL_TEMPLATES[key], HTML_TEMPLATES[key], key


def _render_email(lead: Lead, template_key: str, unsubscribe_url: str) -> tuple[str, str, str]:
    """Render subject, text, and HTML for a lead."""
    ctx = {
        "business_name": lead.business_name,
        "owner_name": lead.owner_name or "",
        "sender_name": SENDER_NAME,
        "unsubscribe_url": unsubscribe_url,
    }
    text = Template(EMAIL_TEMPLATES[template_key]).render(**ctx)
    html = Template(HTML_TEMPLATES[template_key]).render(**ctx)
    subject = f"Quick question about {lead.business_name}"
    return subject, text, html


# ── Outreach Worker ────────────────────────────────────────────────────────────

@shared_task(bind=True, name="app.workers.outreach.tasks.send_email_sequence")
def send_email_sequence(self, batch_size: int = 10) -> dict:
    """
    Pick up outreach_queued leads, create email sequences, and send first touch.
    
    Rate limiting:
    - Max EMAIL_DAILY_LIMIT emails per day
    - Min EMAIL_MIN_GAP_HOURS between touches to same lead
    - At most batch_size emails per Celery run
    """
    daily_limit = settings.email_daily_limit or 5
    min_gap_hours = settings.email_min_gap_hours or 72

    with sync_session_scope() as session:
        # Check today's sends
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

        # Get leads ready for outreach
        min_gap = datetime.now(timezone.utc) - timedelta(hours=min_gap_hours)
        leads = session.execute(
            select(Lead).where(
                Lead.status == "outreach_queued",
                Lead.email.isnot(None),
                # Not contacted in last min_gap_hours
                (Lead.last_contacted_at.is_(None)) | (Lead.last_contacted_at < min_gap),
                # Not already in active sequence
                ~Lead.id.in_(
                    select(OutreachSequence.lead_id).where(
                        OutreachSequence.status.in_(["pending", "sent"]),
                    )
                ),
            ).limit(to_send)
        ).scalars().all()

    if not leads:
        log.info("outreach_no_leads_ready")
        return {"status": "ok", "sent": 0, "reason": "no_leads_ready"}

    log.info("outreach_sending_batch", count=len(leads))
    sent = failed = 0

    try:
        smtp = _make_smtp()
    except Exception as e:
        log.error("smtp_connection_failed", error=str(e))
        return {"status": "error", "error": f"smtp_failed: {e}"}

    for lead in leads:
        if not lead.email:
            continue

        try:
            template_key = _get_template(lead)[2]
            unsub_url = f"https://cc-leadgen.clientcompass.co.za/unsubscribe?token={lead.id}"
            subject, text_body, html_body = _render_email(lead, template_key, unsub_url)

            msg = _build_email(
                from_email=settings.smtp_from_email,
                to_email=lead.email,
                subject=subject,
                html_body=html_body,
                text_body=text_body,
            )

            smtp.sendmail(settings.smtp_from_email, [lead.email], msg.as_string())

            # Record sequence
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

                    # Event log
                    event = LeadEvent(
                        lead_id=lead.id,
                        event_type="email_sent",
                        payload={"subject": subject, "step": 1, "template": template_key},
                    )
                    session.add(event)

            sent += 1
            log.info("outreach_email_sent", lead_id=str(lead.id), email=lead.email)
            time.sleep(2)  # Space out sends

        except Exception as e:
            log.error("outreach_send_failed", lead_id=str(lead.id), error=str(e))
            failed += 1
            continue

    try:
        smtp.quit()
    except Exception:
        pass

    log.info("outreach_batch_done", sent=sent, failed=failed)
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
    inbox = "INBOX"

    replies_found = 0

    try:
        mail = imaplib.IMAP4_SSL(imap_host)
        mail.login(imap_user, imap_pass)
        mail.select(inbox)

        # Search for recent unread emails (new replies)
        status, msg_ids = mail.search(None, "UNSEEN")
        if status != "OK":
            return {"status": "error", "reason": "imap_search_failed"}

        for msg_id in msg_ids[0].split():
            try:
                status, msg_data = mail.fetch(msg_id, "(RFC822)")
                if status != "OK":
                    continue

                raw_email = msg_data[0][1]
                # Parse headers — in real impl use email.message_from_bytes
                # For now, just mark as read (will implement full parsing next)
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
        # TODO: Discord alert on reply

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
            )
        ).scalars().all()

        count = 0
        for lead in updated:
            lead.status = "outreach_queued"
            session.add(lead)
            count += 1

        log.info("outreach_queue_updated", count=count)
        return {"status": "ok", "queued": count}