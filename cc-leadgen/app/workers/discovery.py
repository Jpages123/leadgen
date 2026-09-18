"""Discovery worker tasks — scrapes Google Places (primary), yellsa + cylex (legacy/disabled).
Sources:
  google_places — ✅ Primary. Returns leads with website URLs. Requires GOOGLE_PLACES_API_KEY.
  yellsa        — ❌ Dead (pivoted to Cameroon). Kept for reference but skipped by default.
  cylex         — ❌ Dead (Cloudflare 403). Kept for reference but skipped by default.
"""
from __future__ import annotations
from datetime import datetime, timezone
from typing import Union
import sqlalchemy as sa
from celery import shared_task
from app.config import get_settings
from app.db.sync_session import sync_session_scope
from app.models import DiscoveryJob, Lead
from app.scrapers import (
    CylexLead,
    GooglePlacesLead,
    YellsaLead,
    run_cylex_discovery,
    run_google_places_discovery,
    run_yellsa_discovery,
)
from app.scrapers.base import classify_business_type, infer_city_and_province, normalise_phone
from app.utils.app_settings import get_bool_setting
from app.utils.logger import get_logger
from app.utils.rejected_websites import is_rejected, refresh

log = get_logger(__name__)
# Google Places verticals — web revamp ICP (priority order from WEB_REVAMP_ENGINE.md)
DEFAULT_VERTICALS = [
    "plumber",
    "electrician",
    "builder",
    "cleaning service",
    "photographer",
    "event planner",
]
DEFAULT_CITIES = ["Cape Town", "Johannesburg", "Durban", "Pretoria", "Port Elizabeth", "Bloemfontein", "East London", "Nelspruit", "Polokwane", "Pietermaritzburg", "George", "Kimberley"]
DEFAULT_MAX_PAGES = 3  # 3 pages × 20 results = up to 60 per vertical/city combo
# ── Deduplication ─────────────────────────────────────────────────────────────
LeadInput = Union[YellsaLead, CylexLead, GooglePlacesLead]
def _build_dedup_key(lead: LeadInput) -> str:
    """Build a deduplication key: normalised phone OR (name_lower + city_lower)."""
    phone = getattr(lead, "phone", None) or getattr(lead, "whatsapp_number", None)
    if phone:
        norm = normalise_phone(phone)
        if norm:
            return f"phone:{norm}"
    name = (getattr(lead, "business_name", None) or "").lower().strip()
    city = (getattr(lead, "city", None) or "").lower().strip()
    if name and city:
        return f"name:{name[:80]}|{city[:60]}"
    if name:
        return f"name:{name[:80]}"
    return ""
def _lead_to_row(data: LeadInput) -> dict:
    """Convert scraper dataclass to Lead insert dict."""
    if isinstance(data, YellsaLead):
        source = "yellsa"
    elif isinstance(data, CylexLead):
        source = "cylex"
    else:
        source = "google_places"
    city_raw = getattr(data, "city", None) or ""
    if isinstance(data, GooglePlacesLead):
        # Google Places already has city + province parsed
        city = data.city
        province = data.province
    else:
        city, province = infer_city_and_province(city_raw)
    biz_type = getattr(data, "business_type", None) or classify_business_type(data.business_name)
    row = {
        "source": source,
        "source_url": getattr(data, "source_url", None),
        "business_name": data.business_name,
        "phone": getattr(data, "phone", None),
        "email": getattr(data, "email", None),
        "website": getattr(data, "website", None),
        "city": city,
        "province": province,
        "business_type": biz_type,
        "status": "discovered",
        "discovered_at": datetime.now(timezone.utc),
    }
    # Google Places extras
    if isinstance(data, GooglePlacesLead):
        row["google_rating"] = data.google_rating
        row["google_review_count"] = data.google_review_count
    return row
# ── Update discovery job ───────────────────────────────────────────────────────
def _mark_job_done(job_id: str | None, source: str, leads_found: int, leads_new: int,
                   error: str | None = None) -> None:
    """Update discovery_jobs row with final results."""
    if not job_id:
        return
    try:
        with sync_session_scope() as session:
            job = session.get(DiscoveryJob, job_id)
            if job:
                job.status = "failed" if error else "done"
                job.leads_found = leads_found
                job.leads_new = leads_new
                job.completed_at = datetime.now(timezone.utc).isoformat()[:30]
                if error:
                    job.error_message = error[:500]
                session.add(job)
    except Exception as exc:
        log.error("discovery_job_update_failed", job_id=job_id, error=str(exc))
# ── Main task ─────────────────────────────────────────────────────────────────
@shared_task(bind=True, name="app.workers.discovery.tasks.run_daily_discovery")
def run_daily_discovery(
    self,
    job_id: str | None = None,
    vertical: str | None = None,
    sources: list[str] | None = None,
    max_pages: int | None = None,
) -> dict:
    """Run lead discovery.
    Args:
        job_id: discovery_jobs.id to update with results.
        vertical: Single vertical to focus on (None = all defaults).
        sources: List of sources to run. Defaults to ['google_places'].
                 Pass ['yellsa', 'cylex'] only for legacy testing.
        max_pages: Max pages per source/city combo.
    """
    settings = get_settings()

    # Admin-toggleable kill switch (2026-08-28): Google Places API budget
    # running low, and there was no way to pause daily discovery short of
    # editing code + redeploying. Toggled from the Leadgen Flow admin page
    # (Discovery card) — see app/utils/app_settings.py.
    if not get_bool_setting("discovery_enabled", default=True):
        log.info("discovery_run_skipped", reason="disabled_via_admin_toggle", job_id=job_id)
        return {"status": "ok", "found": 0, "new": 0, "reason": "disabled_via_admin_toggle"}

    sources = sources or ["google_places"]
    verticals = [vertical] if vertical else DEFAULT_VERTICALS
    max_pages = max_pages or DEFAULT_MAX_PAGES
    log.info("discovery_run_start", job_id=job_id, verticals=verticals, sources=sources)
    # Refresh rejected-websites cache so newly added entries take effect.
    refresh()
    total_found = 0
    total_new = 0
    errors: list[str] = []
    for source in sources:
        try:
            if source == "google_places":
                results = run_google_places_discovery(
                    verticals=verticals,
                    cities=DEFAULT_CITIES,
                    max_pages=max_pages,
                )
                for result in results:
                    if result.error:
                        errors.append(f"google_places/{result.vertical}/{result.city}: {result.error}")
                        continue
                    log.info(
                        "discovery_batch_done",
                        source=source,
                        vertical=result.vertical,
                        city=result.city,
                        pages=result.pages_fetched,
                        leads=len(result.leads),
                    )
                    new_leads, skipped = _upsert_leads(result.leads, source)
                    total_found += len(result.leads)
                    total_new += new_leads
                    log.info(
                        "discovery_upsert_done",
                        source=source,
                        vertical=result.vertical,
                        city=result.city,
                        new=new_leads,
                        skipped=skipped,
                    )
            elif source == "yellsa":
                results = run_yellsa_discovery(
                    verticals=verticals,
                    cities=[c.lower().replace(" ", "-") for c in DEFAULT_CITIES],
                    max_pages=max_pages,
                )
                for result in results:
                    if result.error:
                        errors.append(f"yellsa/{result.category}/{result.city}: {result.error}")
                        continue
                    new_leads, skipped = _upsert_leads(result.leads, source)
                    total_found += len(result.leads)
                    total_new += new_leads
            elif source == "cylex":
                results = run_cylex_discovery(
                    verticals=verticals,
                    cities=[c.lower().replace(" ", "-") for c in DEFAULT_CITIES],
                    max_pages=max_pages,
                )
                for result in results:
                    if result.error:
                        errors.append(f"cylex/{result.category}/{result.city}: {result.error}")
                        continue
                    new_leads, skipped = _upsert_leads(result.leads, source)
                    total_found += len(result.leads)
                    total_new += new_leads
            else:
                log.warning("discovery_unknown_source", source=source)
        except Exception as exc:
            log.error("discovery_source_failed", source=source, error=str(exc))
            errors.append(f"{source}: {exc}")
    final_error = "; ".join(errors[:5]) if errors else None
    if job_id:
        _mark_job_done(job_id, ",".join(sources), total_found, total_new, final_error)
    log.info("discovery_run_done", found=total_found, new=total_new, errors=len(errors))
    return {
        "status": "ok",
        "job_id": job_id,
        "leads_found": total_found,
        "leads_new": total_new,
        "errors": errors,
    }
def _upsert_leads(leads: list[LeadInput], source: str) -> tuple[int, int]:
    """Deduplicate incoming leads against DB and insert new ones.
    Returns (new_count, skipped_count).
    """
    if not leads:
        return 0, 0
    incoming_keys: dict[str, LeadInput] = {}
    for lead in leads:
        key = _build_dedup_key(lead)
        if key and key not in incoming_keys:
            incoming_keys[key] = lead
    if not incoming_keys:
        return 0, 0
    with sync_session_scope() as session:
        existing_keys: set[str] = set()
        for key in incoming_keys:
            if key.startswith("phone:"):
                phone_val = key.replace("phone:", "")
                exists = session.query(sa.func.count(Lead.id)).filter(
                    (Lead.phone == phone_val) | (Lead.whatsapp_number == phone_val)
                ).scalar() > 0
                if exists:
                    existing_keys.add(key)
            else:
                name_part = key.replace("name:", "").split("|")[0]
                city_part = key.split("|")[1] if "|" in key else ""
                q = session.query(sa.func.count(Lead.id)).filter(
                    sa.func.lower(Lead.business_name) == name_part
                )
                if city_part:
                    q = q.filter(sa.func.lower(Lead.city) == city_part)
                if q.scalar() > 0:
                    existing_keys.add(key)
        new_keys = set(incoming_keys) - existing_keys
        new_count = 0
        skipped_rejected = 0
        for key in new_keys:
            lead_data = incoming_keys[key]
            # ── Rejected-website filter ────────────────────────────────
            # Franchise leads often share one website across many locations.
            # Skip any lead whose website domain matches a rejected entry.
            website = getattr(lead_data, "website", None)
            if website and is_rejected(website):
                skipped_rejected += 1
                continue
            # ─────────────────────────────────────────────────────────
            row = _lead_to_row(lead_data)
            session.add(Lead(**row))
            new_count += 1
        if skipped_rejected:
            log.info("discovery_rejected_websites_filtered", count=skipped_rejected)
        return new_count, len(existing_keys)
