"""Email draft worker — generates email drafts for approved mockups and sends them.

Two Celery tasks:
  - generate_email_draft(approval_id)
      Reads the approval row from prod admin DB (admin_crm.mockup_approvals),
      pulls the lead data from leadgen DB, builds the email content via
      app.utils.email_builder, and writes a row into admin_crm.email_drafts.

  - send_email_draft(draft_id)
      Reads the (operator-edited) draft from prod DB, sends via SMTP,
      updates status to 'sent' or 'failed'.

The sync task (app.workers.mockup_approval_sync.sync_approvals) is extended
to also trigger generate_email_draft for newly-approved mockups.

Manual callers can also invoke these directly via:
  from app.workers.email_draft import generate_email_draft
  generate_email_draft.delay('<approval_id>')
"""
from __future__ import annotations

import json
import smtplib
import uuid
from datetime import datetime, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.application import MIMEApplication
from email.mime.text import MIMEText
from urllib.parse import urlparse, unquote

import psycopg2
from celery import shared_task

from app.config import get_settings
from app.db.sync_session import sync_session_scope
from app.models import Lead
from app.utils.email_builder import render_email_for_lead
from app.utils.email_modern_builder import render_modern_web_revamp
from app.utils.mockup_screenshot import capture_and_upload_screenshot
from app.utils.logger import get_logger

log = get_logger(__name__)
settings = get_settings()


def _parse_prod_db_url(raw: str) -> dict:
    """Parse PROD_DB_URL safely (handles @ in password)."""
    cleaned = raw.replace("+asyncpg", "")
    parsed = urlparse(cleaned)
    return {
        "user": unquote(parsed.username or ""),
        "password": unquote(parsed.password or ""),
        "host": parsed.hostname,
        "port": parsed.port or 5432,
        "database": (parsed.path or "/").lstrip("/"),
    }


def _get_prod_conn():
    """Return a psycopg2 connection to the prod DB."""
    prod_url = settings.prod_db_url
    if not prod_url:
        raise RuntimeError("PROD_DB_URL not configured")
    cfg = _parse_prod_db_url(prod_url)
    return psycopg2.connect(
        user=cfg["user"], password=cfg["password"],
        host=cfg["host"], port=cfg["port"], dbname=cfg["database"],
        connect_timeout=10,
    )


def _has_active_draft(conn, approval_id: str) -> bool:
    """Return True if the approval already has a non-final draft.

    A draft is "active" if its status is pending, sent, or no_contact.
    The sync task uses this to prevent creating duplicate drafts every 5 min.

    Final states (discarded, failed) do NOT block regeneration — operator
    can re-trigger by approving again or by calling the task directly.
    """
    with conn.cursor() as cur:
        cur.execute("""
            SELECT 1 FROM admin_crm.email_drafts
            WHERE approval_id = %s
              AND status IN ('pending', 'sent', 'no_contact')
            LIMIT 1
        """, (approval_id,))
        return cur.fetchone() is not None


@shared_task(bind=True, name="app.workers.email_draft.tasks.generate_email_draft")
def generate_email_draft(self, approval_id: str) -> dict:
    """Build an email draft for an approved mockup and write it to prod DB.

    Idempotent — if an active draft already exists for this approval_id,
    returns without changes.
    """
    log.info("draft_generation_started", approval_id=approval_id)

    # 1) Read approval row from prod DB
    try:
        conn = _get_prod_conn()
    except Exception as exc:
        log.warning("draft_prod_db_unavailable", error=str(exc))
        return {"status": "skipped", "reason": str(exc)}

    try:
        with conn.cursor() as cur:
            cur.execute("SET LOCAL app.is_admin = 'true'")
            cur.execute("""
                SELECT id, lead_id, business_name, mockup_url, status
                FROM admin_crm.mockup_approvals
                WHERE id = %s
            """, (approval_id,))
            row = cur.fetchone()
    finally:
        conn.close()

    if not row:
        log.warning("draft_approval_not_found", approval_id=approval_id)
        return {"status": "skipped", "reason": "approval_not_found"}

    db_approval_id, lead_id, business_name, mockup_url, approval_status = row

    if approval_status != "approved":
        log.info("draft_approval_not_approved", approval_id=approval_id, status=approval_status)
        return {"status": "skipped", "reason": f"approval_status={approval_status}"}

    # 2) Check for existing active draft
    try:
        conn = _get_prod_conn()
        if _has_active_draft(conn, db_approval_id):
            conn.close()
            log.info("draft_already_exists", approval_id=db_approval_id)
            return {"status": "skipped", "reason": "active_draft_exists"}
        conn.close()
    except Exception as exc:
        log.warning("draft_active_check_failed", error=str(exc))
        # Continue anyway — worst case is a duplicate that we can dedupe later

    # 3) Read lead from leadgen DB
    with sync_session_scope() as session:
        lead = session.get(Lead, lead_id)
        if not lead:
            log.error("draft_lead_not_found", lead_id=lead_id)
            return {"status": "skipped", "reason": "lead_not_found"}

        # Use a detached snapshot — we don't want session state attached across
        # the cross-DB write
        lead_snapshot = {
            "id": str(lead.id),
            "business_name": lead.business_name,
            "owner_name": lead.owner_name,
            "email": lead.email,
            "business_type": lead.business_type,
            "website_platform": lead.website_platform,
            "pagespeed_mobile": lead.pagespeed_mobile,
            "web_pitch_score": lead.web_pitch_score,
            "web_audit_pdf_path": lead.web_audit_pdf_path,
            "mockup_url": lead.mockup_url,
            "mockup_status": lead.mockup_status,
        }

    # 4) Build email content. For mockup-approved leads, use the modern
    #    Stripe/Linear-style renderer with inline screenshot. Otherwise fall
    #    back to the plain text renderer.
    # We construct a lightweight shim with the attributes the builder reads
    class _LeadShim:
        pass

    shim = _LeadShim()
    for k, v in lead_snapshot.items():
        setattr(shim, k, v)

    screenshot_ref = None
    screenshot_is_url = False
    if lead_snapshot.get("mockup_url") and lead_snapshot.get("mockup_status") == "approved":
        # Capture screenshot of the live mockup and upload to VPS for
        # email-client compatibility (Gmail strips inline base64 images).
        screenshot_ref, screenshot_is_url = capture_and_upload_screenshot(
            lead_snapshot["mockup_url"],
            lead_id=lead_id,
            max_width=500,
            jpeg_quality=65,
        )
        log.info(
            "screenshot_processed",
            lead_id=lead_id,
            is_url=screenshot_is_url,
            has_ref=bool(screenshot_ref),
        )
        # Use modern renderer (falls back to dark CTA button if no screenshot)
        content = render_modern_web_revamp(shim, screenshot_ref=screenshot_ref)
        log.info(
            "draft_using_modern_template",
            lead_id=lead_id,
            has_screenshot=bool(screenshot_ref),
            screenshot_is_url=screenshot_is_url,
        )
    else:
        content = render_email_for_lead(shim)

    # 5) Determine draft status — if no email, mark as no_contact
    initial_status = "pending" if lead_snapshot["email"] else "no_contact"

    # 6) Insert draft row in prod DB
    try:
        conn = _get_prod_conn()
        conn.autocommit = False
        try:
            with conn.cursor() as cur:
                cur.execute("SET LOCAL app.is_admin = 'true'")
                cur.execute("""
                    INSERT INTO admin_crm.email_drafts (
                        approval_id, lead_id, business_name,
                        to_email, from_email,
                        subject, body_html, body_text,
                        attachments, status, template_key,
                        created_at, updated_at
                    ) VALUES (
                        %s, %s, %s,
                        %s, %s,
                        %s, %s, %s,
                        %s::jsonb, %s, %s,
                        NOW(), NOW()
                    )
                    RETURNING id
                """, (
                    db_approval_id,
                    lead_id,
                    lead_snapshot["business_name"],
                    lead_snapshot["email"],
                    settings.smtp_from_email,
                    content.subject,
                    content.body_html,
                    content.body_text,
                    json.dumps(content.attachments),
                    initial_status,
                    content.template_key,
                ))
                draft_id = cur.fetchone()[0]
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()
    except Exception as exc:
        log.error("draft_insert_failed", error=str(exc), approval_id=approval_id)
        return {"status": "error", "error": str(exc)}

    log.info(
        "draft_generated",
        draft_id=str(draft_id),
        approval_id=db_approval_id,
        lead_id=lead_id,
        business_name=lead_snapshot["business_name"],
        template_key=content.template_key,
        initial_status=initial_status,
    )
    return {
        "status": "ok",
        "draft_id": str(draft_id),
        "approval_id": str(db_approval_id),
        "initial_status": initial_status,
        "template_key": content.template_key,
    }


@shared_task(bind=True, name="app.workers.email_draft.tasks.send_email_draft")
def send_email_draft(self, draft_id: str) -> dict:
    """Send the operator-approved email draft via SMTP.

    Reads the (potentially operator-edited) draft row from prod DB and sends.
    Updates status to 'sent' on success, 'failed' on SMTP error.
    """
    log.info("draft_send_started", draft_id=draft_id)

    # 1) Read draft from prod DB
    try:
        conn = _get_prod_conn()
        with conn.cursor() as cur:
            cur.execute("SET LOCAL app.is_admin = 'true'")
            cur.execute("""
                SELECT id, approval_id, lead_id, business_name,
                       to_email, from_email,
                       subject, body_html, body_text,
                       attachments, status, template_key
                FROM admin_crm.email_drafts
                WHERE id = %s
            """, (draft_id,))
            row = cur.fetchone()
        conn.close()
    except Exception as exc:
        log.error("draft_read_failed", draft_id=draft_id, error=str(exc))
        return {"status": "error", "error": str(exc)}

    if not row:
        log.warning("draft_not_found", draft_id=draft_id)
        return {"status": "skipped", "reason": "draft_not_found"}

    (db_id, approval_id, lead_id, business_name,
     to_email, from_email,
     subject, body_html, body_text,
     attachments, status, template_key) = row

    # 1b) Fetch the lead record from leadgen DB so we can regenerate the PDF
    # on the fly if the durable location doesn't have it (Session 15 fix —
    # pre-fix the email was silently sent without the attachment).
    lead = None
    if lead_id:
        try:
            with sync_session_scope() as session:
                lead = session.get(Lead, lead_id)
        except Exception as exc:
            log.warning("draft_lead_fetch_failed", draft_id=draft_id, lead_id=str(lead_id), error=str(exc))

    if status != "pending":
        log.info("draft_send_skipped_not_pending", draft_id=draft_id, status=status)
        return {"status": "skipped", "reason": f"status={status}"}

    if not to_email:
        # Operator hasn't filled in the email yet
        log.warning("draft_send_no_email", draft_id=draft_id)
        return {"status": "skipped", "reason": "no_to_email"}

    # 2) Build the MIME message (re-using logic from outreach)
    msg = MIMEMultipart("mixed")
    msg["From"] = f"{settings.smtp_from_name} <{from_email}>"
    msg["To"] = to_email
    msg["Subject"] = subject
    msg["Message-ID"] = f"<{uuid.uuid4().hex}@outreach.clientcompass.co.za>"

    alt = MIMEMultipart("alternative")
    alt.attach(MIMEText(body_text, "plain", "utf-8"))
    # Tracking pixel — same as outreach._build_email
    import hashlib as _hashlib
    tracking_pixel = (
        f'<img src="https://cc-leadgen.clientcompass.co.za/track/'
        f'{_hashlib.md5(to_email.encode()).hexdigest()}.png" width="1" height="1" />'
    )
    html_with_tracking = body_html.replace("</body>", tracking_pixel + "</body>")
    alt.attach(MIMEText(html_with_tracking, "html", "utf-8"))
    msg.attach(alt)

    # 3) Attach PDF — durable location preferred, legacy fallback, on-demand regen if missing
    #
    # The DB column ``web_audit_pdf_path`` was historically written to the
    # ephemeral ``/tmp/cc_reports/`` directory. Session 9/15 cleanup moved new
    # writes to ``<project>/.cache/reports/`` via ``report_assets``, but
    # existing rows still point at the old path which may or may not still
    # exist. Resolve through ``report_assets.resolve`` (durable → legacy
    # fallback) and if that fails, regenerate from the lead record we have
    # on hand. The result is that the prospect always receives a PDF as
    # long as the lead was audited at least once — silently dropping the
    # attachment (the pre-fix behaviour) is no longer possible.
    from pathlib import Path as _Path
    pdf_path_raw = (attachments or {}).get("pdf_path") if isinstance(attachments, dict) else None
    if not pdf_path_raw and isinstance(attachments, str):
        try:
            pdf_path_raw = json.loads(attachments).get("pdf_path")
        except Exception:
            pdf_path_raw = None

    resolved_pdf: "_Path | None" = None
    if pdf_path_raw:
        try:
            from app.utils.report_assets import resolve as _resolve_report
            stem = _Path(pdf_path_raw).stem  # e.g. "limelight-event-hire"
            resolved_pdf = _resolve_report(stem)
        except Exception as exc:
            log.warning("draft_pdf_resolve_failed", path=pdf_path_raw, error=str(exc))

    if resolved_pdf is None and lead is not None:
        try:
            from app.utils.report_assets import regenerate_pdf_for_lead
            regenerated = regenerate_pdf_for_lead(lead)
            if regenerated:
                resolved_pdf = _Path(regenerated)
                log.info("draft_pdf_regen_inline", path=regenerated)
        except Exception as exc:
            log.warning("draft_pdf_regen_failed", error=str(exc))

    if resolved_pdf and resolved_pdf.exists() and resolved_pdf.stat().st_size > 0:
        try:
            import os
            with open(resolved_pdf, "rb") as f:
                pdf_data = f.read()
            filename = os.path.basename(resolved_pdf).replace("-", "_")
            pdf_part = MIMEApplication(pdf_data, _subtype="pdf")
            pdf_part.add_header("Content-Disposition", "attachment", filename=f"web_audit_{filename}")
            msg.attach(pdf_part)
        except Exception as exc:
            log.warning("draft_pdf_attach_failed", path=str(resolved_pdf), error=str(exc))
    elif pdf_path_raw:
        log.warning("draft_pdf_attach_skipped", reason="missing_or_empty", original_path=pdf_path_raw)

    # 4) Send via SMTP
    try:
        smtp = smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=20)
        smtp.starttls()
        smtp.login(settings.smtp_user, settings.smtp_pass)
        smtp.sendmail(from_email, [to_email], msg.as_string())
        smtp.quit()
    except Exception as exc:
        log.error("draft_send_smtp_failed", draft_id=draft_id, error=str(exc))
        _update_draft_status(draft_id, "failed", error=str(exc)[:500])
        return {"status": "error", "error": str(exc)}

    # 5) Update draft status to 'sent'
    _update_draft_status(draft_id, "sent", sent_at=True)

    # 6) Write outreach event to leadgen DB for reply-tracking continuity
    try:
        with sync_session_scope() as session:
            from app.models import LeadEvent, OutreachSequence
            seq = OutreachSequence(
                lead_id=lead_id,
                channel="email",
                sequence_name=f"{template_key}_v1",
                step_number=1,
                subject=subject,
                message_body=body_text,
                status="sent",
                sent_at=datetime.now(timezone.utc),
                message_id=msg["Message-ID"],
            )
            session.add(seq)
            event = LeadEvent(
                lead_id=lead_id,
                event_type="email_sent",
                payload={
                    "subject": subject,
                    "step": 1,
                    "template": template_key,
                    "draft_id": draft_id,
                    "pdf_attached": bool(pdf_path),
                    "via_draft": True,
                },
            )
            session.add(event)
            # Update lead.last_contacted_at + status
            lead_db = session.get(Lead, lead_id)
            if lead_db:
                lead_db.last_contacted_at = datetime.now(timezone.utc)
                lead_db.status = "contacted"
                session.add(lead_db)
    except Exception as exc:
        log.warning("draft_leadgen_writeback_failed", error=str(exc))
        # Email was sent — don't fail the task just because the local writeback failed

    log.info("draft_send_succeeded", draft_id=draft_id, lead_id=lead_id, to_email=to_email)
    return {
        "status": "ok",
        "draft_id": draft_id,
        "to_email": to_email,
        "subject": subject,
    }


def _update_draft_status(draft_id: str, new_status: str, *, sent_at: bool = False, error: str = None) -> None:
    """Update draft status row in prod DB."""
    try:
        conn = _get_prod_conn()
        conn.autocommit = False
        try:
            with conn.cursor() as cur:
                cur.execute("SET LOCAL app.is_admin = 'true'")
                if sent_at:
                    cur.execute("""
                        UPDATE admin_crm.email_drafts
                        SET status = %s, sent_at = NOW(), updated_at = NOW(), error = NULL
                        WHERE id = %s
                    """, (new_status, draft_id))
                elif error:
                    cur.execute("""
                        UPDATE admin_crm.email_drafts
                        SET status = %s, error = %s, updated_at = NOW()
                        WHERE id = %s
                    """, (new_status, error, draft_id))
                else:
                    cur.execute("""
                        UPDATE admin_crm.email_drafts
                        SET status = %s, updated_at = NOW()
                        WHERE id = %s
                    """, (new_status, draft_id))
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()
    except Exception as exc:
        log.error("draft_status_update_failed", draft_id=draft_id, error=str(exc))