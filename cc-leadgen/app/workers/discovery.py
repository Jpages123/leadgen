"""Discovery worker tasks — scrapes yellsa + cylex, deduplicates, saves to DB."""
from __future__ import annotations

from datetime import datetime, timezone

import sqlalchemy as sa
from celery import shared_task

from app.config import get_settings
from app.db.sync_session import sync_session_scope
from app.models import DiscoveryJob, Lead
from app.scrapers import (
    CylexLead,
    YellsaLead,
    run_cylex_discovery,
    run_yellsa_discovery,
)
from app.scrapers.base import classify_business_type, infer_city_and_province, normalise_phone
from app.utils.logger import get_logger

log = get_logger(__name__)

DEFAULT_VERTICALS = ["hair salon", "nail salon", "beauty salon"]
DEFAULT_CITIES = ["cape-town", "johannesburg", "durban", "pretoria", "port-elizabeth"]
DEFAULT_MAX_PAGES = 10


# ── Deduplication ─────────────────────────────────────────────────────────────


def _build_dedup_key(lead: Lead | YellsaLead | CylexLead) -> str:
    """Build a deduplication key: normalised phone OR (name_lower + city_lower).

    Both scrapers and DB leads use the same key so we can cross-dedup.
    """
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


def _lead_to_row(data: YellsaLead | CylexLead) -> dict:
    """Convert scraper dataclass to Lead insert dict."""
    city, province = infer_city_and_province(data.city or "")
    biz_type = data.business_type or classify_business_type(data.business_name)

    return {
        "source": "yellsa" if isinstance(data, YellsaLead) else "cylex",
        "source_url": data.source_url,
        "business_name": data.business_name,
        "phone": data.phone,
        "email": data.email,
        "website": data.website,
        "city": city,
        "province": province,
        "business_type": biz_type,
        "status": "discovered",
        "discovered_at": datetime.now(timezone.utc),
    }


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
    """Run lead discovery across yellsa + cylex.

    Args:
        job_id: discovery_jobs.id to update with results.
        vertical: Single vertical to focus on (None = all top 3).
        sources: List of sources to run ('yellsa', 'cylex'). None = both.
        max_pages: Max pages per source/city combo.
    """
    settings = get_settings()
    sources = sources or ["yellsa", "cylex"]
    verticals = [vertical] if vertical else DEFAULT_VERTICALS
    max_pages = max_pages or settings.yellsa_max_pages
    delay_between = 1.0  # polite delay between category/city combos

    log.info("discovery_run_start", job_id=job_id, verticals=verticals, sources=sources)

    total_found = 0
    total_new = 0
    errors: list[str] = []

    for vertical in verticals:
        for source in sources:
            try:
                if source == "yellsa":
                    results = run_yellsa_discovery(
                        verticals=[vertical],
                        cities=DEFAULT_CITIES,
                        max_pages=max_pages,
                    )
                elif source == "cylex":
                    results = run_cylex_discovery(
                        verticals=[vertical],
                        cities=DEFAULT_CITIES,
                        max_pages=max_pages,
                    )
                else:
                    continue

                for result in results:
                    if result.error:
                        errors.append(f"{source}/{result.category}/{result.city}: {result.error}")
                        continue

                    log.info(
                        "discovery_batch_done",
                        source=source,
                        category=result.category,
                        city=result.city,
                        pages=result.pages_scraped,
                        leads=len(result.leads),
                    )

                    # Deduplicate + save to DB
                    new_leads, skipped = _upsert_leads(result.leads, source)
                    total_found += len(result.leads)
                    total_new += new_leads

                    log.info(
                        "discovery_upsert_done",
                        source=source,
                        city=result.city,
                        new=new_leads,
                        skipped=skipped,
                    )

            except Exception as exc:
                log.error("discovery_source_failed", source=source, error=str(exc))
                errors.append(f"{source}: {exc}")

    # Mark job done
    final_error = "; ".join(errors[:5]) if errors else None
    if job_id:
        _mark_job_done(job_id, "multi", total_found, total_new, final_error)

    log.info("discovery_run_done", found=total_found, new=total_new, errors=len(errors))

    return {
        "status": "ok",
        "job_id": job_id,
        "leads_found": total_found,
        "leads_new": total_new,
        "errors": errors,
    }


def _upsert_leads(leads: list[YellsaLead | CylexLead], source: str) -> tuple[int, int]:
    """Deduplicate incoming leads against DB and insert new ones.

    Returns (new_count, skipped_count).
    """
    if not leads:
        return 0, 0

    # Build dedup keys for incoming
    incoming_keys: dict[str, YellsaLead | CylexLead] = {}
    for lead in leads:
        key = _build_dedup_key(lead)
        if key and key not in incoming_keys:
            incoming_keys[key] = lead

    if not incoming_keys:
        return 0, 0

    with sync_session_scope() as session:
        # Fetch existing keys from DB (only needed for dedup)
        existing_keys: set[str] = set()
        for key in incoming_keys:
            if key.startswith("phone:"):
                # Check by normalised phone
                phone_val = key.replace("phone:", "")
                exists = session.query(sa.func.count(Lead.id)).filter(
                    (Lead.phone == phone_val) | (Lead.whatsapp_number == phone_val)
                ).scalar() > 0
                if exists:
                    existing_keys.add(key)
            else:
                # Check by name + city
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
        for key in new_keys:
            lead_data = incoming_keys[key]
            row = _lead_to_row(lead_data)
            session.add(Lead(**row))
            new_count += 1

        return new_count, len(existing_keys)