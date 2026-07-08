"""Enrichment worker — enriches leads via Yep Mall API and website crawl."""
from __future__ import annotations

import re
import time
from typing import Optional

import requests
from celery import shared_task

from app.config import get_settings
from app.db.sync_session import sync_session_scope
from app.models import Lead
from app.utils.logger import get_logger

log = get_logger(__name__)

# ── Yep Mall Detail API ───────────────────────────────────────────────────────
API_DETAIL = "https://fm.mall.yep.co.za/api/seller/detail"
HEADERS = {
    "Content-Type": "application/json",
    "Referer": "https://mall.yep.co.za/",
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36",
}


def _normalise_sa_phone(raw: str) -> Optional[str]:
    """Convert SA number to E.164 +27 format, or return None."""
    if not raw:
        return None
    digits = "".join(filter(str.isdigit, str(raw)))
    if len(digits) == 0:
        return None
    if digits.startswith("0") and len(digits) == 10:
        return f"+27{digits[1:]}"
    if len(digits) == 9 and digits[0] in "6789":
        return f"+27{digits}"
    if digits.startswith("27") and len(digits) == 11:
        return f"+{digits}"
    return None


def _enrich_from_detail(seller_id: int) -> Optional[dict]:
    """Fetch full store details from Yep Mall seller detail API."""
    try:
        resp = requests.post(
            API_DETAIL,
            headers=HEADERS,
            json={"sellerId": seller_id},
            timeout=15,
        )
        if resp.status_code != 200:
            return None
        data = resp.json()
        if not data.get("success"):
            return None
        return data.get("data", {})
    except Exception as e:
        log.warning("detail_api_error", seller_id=seller_id, error=str(e))
        return None


def apply_detail_to_lead(lead: Lead, detail: dict) -> dict:
    """Apply Yep Mall detail data to a lead record.
    
    Returns dict with what was updated.
    """
    updates = {}
    shop = detail.get("sellerShopVO", {})

    # Contact email — prefer contactEmail over email
    email = detail.get("contactEmail", "").strip() or detail.get("email", "").strip()
    if email and not lead.email:
        lead.email = email
        updates["email"] = email

    # Website
    website = detail.get("websiteAddress", "").strip()
    if website and not lead.website:
        if not website.startswith("http"):
            website = "https://" + website
        lead.website = website
        updates["website"] = website

    # WhatsApp / mobile number (from contactMobileNumber or mobileNumber)
    for raw in [detail.get("contactMobileNumber", ""), detail.get("mobileNumber", "")]:
        if raw:
            phone = _normalise_sa_phone(raw)
            if phone and not lead.whatsapp_number:
                lead.whatsapp_number = phone
                updates["whatsapp_number"] = phone
                break

    # Owner / contact name
    contact = detail.get("contactName", "").strip()
    if contact and not lead.owner_name:
        lead.owner_name = contact
        updates["owner_name"] = contact

    # Business type from category
    cats = detail.get("businessCategoryVOList", [])
    if cats:
        cat_names = [c.get("categoryName", "") for c in cats if c.get("categoryName")]
        if cat_names and not getattr(lead, "business_type", None):
            lead.business_type = cat_names[0]
            updates["business_type"] = cat_names[0]

    return updates


# ── Website Crawl (existing logic) ────────────────────────────────────────────
EMAIL_RE = re.compile(r"[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}")
WHATSAPP_RE = re.compile(r'href=["\']?(https?://wa\.me/[0-9]+)["\']?')
INSTAGRAM_RE = re.compile(r'href=["\']?(https?://(?:www\.)?instagram\.com/[^"\'>\s]+)["\']?')
FACEBOOK_RE = re.compile(r'href=["\']?(https?://(?:www\.)?facebook\.com/[^"\'>\s]+)["\']?')

COMPETITOR_KEYWORDS = [
    "wati.io", "getwati", "interakt", "respond.io", "captain.io",
    "megacrm", "chatter.io", "userlike", "trengo",
]


def _fetch_html(url: str, timeout: int = 8) -> Optional[str]:
    try:
        resp = requests.get(
            url,
            headers={"User-Agent": "Mozilla/5.0 (compatible; CCLeadGenBot/1.0; +https://clientcompass.co.za)"},
            timeout=timeout,
            allow_redirects=True,
            verify=True,
        )
        if resp.status_code == 200 and resp.text:
            return resp.text
        return None
    except Exception:
        return None


def _extract_emails(html: str) -> list[str]:
    return list(set(EMAIL_RE.findall(html)))


def _extract_whatsapp(html: str) -> Optional[str]:
    match = WHATSAPP_RE.search(html)
    return match.group(1) if match else None


def _extract_socials(html: str) -> tuple[Optional[str], Optional[str]]:
    ig = INSTAGRAM_RE.search(html)
    fb = FACEBOOK_RE.search(html)
    return (ig.group(1) if ig else None, fb.group(1) if fb else None)


def _is_competitor_customer(html: str) -> bool:
    text = html.lower()
    return any(kw in text for kw in COMPETITOR_KEYWORDS)


def crawl_lead_website(lead: Lead) -> dict:
    """Crawl a lead's website and extract additional contact info."""
    if not lead.website:
        return {"skipped": "no_website", "lead_id": str(lead.id)}

    html = _fetch_html(lead.website)
    if not html:
        log.warning("enrich_crawl_failed", lead_id=str(lead.id), url=lead.website)
        return {"skipped": "crawl_failed", "lead_id": str(lead.id)}

    updates = {}

    emails = _extract_emails(html)
    if emails and not lead.email:
        lead.email = emails[0]
        updates["email"] = emails[0]

    wa_url = _extract_whatsapp(html)
    if wa_url and not lead.whatsapp_number:
        match = re.search(r"wa\.me/(\d+)", wa_url)
        if match:
            phone = _normalise_sa_phone(match.group(1))
            if phone:
                lead.whatsapp_number = phone
                updates["whatsapp_number"] = phone

    ig, fb = _extract_socials(html)
    if ig and not lead.instagram_url:
        lead.instagram_url = ig
        updates["instagram_url"] = ig
    if fb and not lead.facebook_url:
        lead.facebook_url = fb
        updates["facebook_url"] = fb

    if _is_competitor_customer(html) and lead.status == "discovered":
        lead.status = "competitor_customer"
        lead.notes = (lead.notes or "") + "\n[enrich] Uses WhatsApp automation tool (detected on website)"
        updates["competitor_customer"] = True

    return {"skipped": None, "lead_id": str(lead.id), **updates}


# ── Celery Tasks ───────────────────────────────────────────────────────────────




def _maybe_queue_deferred_mockup(lead_id: str, pitch_score: int | None, threshold: int = 70) -> None:
    """If a lead was flagged for deferred mockup during audit, queue it now.

    Called from enrich_website_crawl and enrich_yep_mall_leads after they
    successfully fill in a contact channel. Tier-1 fix (2026-07-08): the
    audit worker sets mockup_eligible_pending_contact=True when pitch ≥
    threshold but no contact info was present at audit time. This helper
    is the second half of that handshake.

    Idempotent: clears the flag before queuing so a second enrichment call
    (e.g. both crawl AND yep_mall fire) doesn't generate two mockups.
    """
    if not pitch_score or pitch_score < threshold:
        return
    try:
        with sync_session_scope() as session:
            db_lead = session.get(Lead, lead_id)
            if not db_lead:
                return
            if not db_lead.mockup_eligible_pending_contact:
                return
            # Sanity re-check: must still have at least one contact channel
            if not (db_lead.email or db_lead.phone or db_lead.whatsapp_number):
                return
            db_lead.mockup_eligible_pending_contact = False
            session.add(db_lead)
        from app.workers.mockup_generator import generate_mockup
        generate_mockup.delay(str(lead_id))
        log.info(
            "deferred_mockup_queued",
            lead_id=lead_id,
            pitch_score=pitch_score,
        )
    except Exception as exc:
        log.warning(
            "deferred_mockup_queue_failed",
            lead_id=lead_id,
            error=str(exc),
        )


@shared_task(bind=True, name="app.workers.enrichment.tasks.enrich_yep_mall_leads")
def enrich_yep_mall_leads(self, batch_size: int = 100, delay: float = 1.0) -> dict:
    """
    Fetch Yep Mall seller details for discovered Yep Mall leads.
    Gets: email, website, whatsapp, owner_name, business_type.
    
    Only processes leads with seller_id set (Yep Mall leads).
    Updates: email, website, whatsapp_number, owner_name, business_type.
    """
    with sync_session_scope() as session:
        leads = (
            session.query(Lead)
            .filter(Lead.source == "yep_mall")
            .filter(Lead.seller_id.isnot(None))
            .filter(
                # Not yet enriched via detail API — missing email OR website
                (Lead.email.is_(None)) | (Lead.website.is_(None))
            )
            .order_by(Lead.discovered_at.asc())
            .limit(batch_size)
            .all()
        )

    if not leads:
        log.info("yep_detail_queue_empty")
        return {"status": "ok", "leads_processed": 0}

    results = []
    processed = 0

    for lead in leads:
        seller_id = lead.seller_id
        if not seller_id:
            processed += 1
            continue

        detail = _enrich_from_detail(seller_id)
        if detail:
            updates = apply_detail_to_lead(lead, detail)
            if updates:
                with sync_session_scope() as session:
                    db_lead = session.get(Lead, lead.id)
                    if db_lead:
                        for k, v in updates.items():
                            setattr(db_lead, k, v)
                        db_lead.detail_fetched = True
                        session.add(db_lead)
                results.append({"seller_id": seller_id, "updates": updates})
                log.info("yep_detail_applied", seller_id=seller_id, updates=list(updates.keys()))
                # Tier-1 fix (2026-07-08): see _maybe_queue_deferred_mockup
                _maybe_queue_deferred_mockup(str(lead.id), lead.web_pitch_score)
        else:
            results.append({"seller_id": seller_id, "error": "detail_fetch_failed"})

        processed += 1
        if processed < len(leads):
            time.sleep(delay)

    log.info(
        "yep_detail_batch_done",
        processed=processed,
        emails_found=sum(1 for r in results if "email" in r.get("updates", {})),
        websites_found=sum(1 for r in results if "website" in r.get("updates", {})),
        wa_found=sum(1 for r in results if "whatsapp_number" in r.get("updates", {})),
    )

    return {
        "status": "ok",
        "leads_processed": processed,
        "results": results,
    }


@shared_task(bind=True, name="app.workers.enrichment.tasks.enrich_website_crawl")
def enrich_website_crawl(self, batch_size: int = 50) -> dict:
    """
    Website crawl for leads that have a website but no email.
    Rate-limit: 2 req/s.
    """
    settings = get_settings()
    rate_limit = settings.google_maps_rate_limit or 2
    delay = 1.0 / rate_limit

    with sync_session_scope() as session:
        leads = (
            session.query(Lead)
            .filter(Lead.status == "discovered")
            .filter(Lead.website.isnot(None))
            .filter(Lead.website != "")
            .filter(Lead.email.is_(None))
            .limit(batch_size)
            .all()
        )

    if not leads:
        log.info("yep_crawl_queue_empty")
        return {"status": "ok", "leads_processed": 0}

    results = []
    processed = 0

    for lead in leads:
        result = crawl_lead_website(lead)
        if result.get("skipped") is None:
            with sync_session_scope() as session:
                db_lead = session.get(Lead, lead.id)
                if db_lead:
                    for k, v in result.items():
                        if k not in ("skipped", "lead_id"):
                            setattr(db_lead, k, v)
                    session.add(db_lead)
            # Tier-1 fix (2026-07-08): if the audit flagged this lead for a
            # deferred mockup, queue it now that we have a contact channel.
            _maybe_queue_deferred_mockup(str(lead.id), lead.web_pitch_score)
        results.append(result)
        processed += 1
        if processed < len(leads):
            time.sleep(delay)

    log.info(
        "yep_crawl_batch_done",
        processed=processed,
        emails_found=sum(1 for r in results if r.get("email")),
    )

    return {"status": "ok", "leads_processed": processed, "results": results}


# ── Legacy task name kept for backwards-compat ────────────────────────────────
@shared_task(bind=True, name="app.workers.enrichment.tasks.process_enrichment_queue")
def process_enrichment_queue(self, batch_size: int = 50) -> dict:
    """Alias — runs both Yep Mall detail fetch and website crawl."""
    r1 = enrich_yep_mall_leads(batch_size=batch_size)
    r2 = enrich_website_crawl(batch_size=batch_size)
    return {"detail": r1, "crawl": r2}