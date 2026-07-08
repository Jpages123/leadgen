"""Web audit worker — runs Playwright + PageSpeed against discovered leads.

Pipeline per lead (audit_lead_task):
  1. preflight_check()  → skip dead sites fast
  2. audit_website()    → platform, copyright year, screenshot, assets,
                          modern-web signals (HTTPS, WhatsApp, JSON-LD, …)
  3. fetch_pagespeed()  → mobile/desktop scores (parallel)
  4. calculate_web_pitch_score()  — V2 with Tier-1 bonuses + Tier-2 dampener
  5. Persist all fields back to leads table
  6. Queue mockup generation if pitch_score >= threshold

V2 scoring (2026-07-08 audit):
  - Tier-1 bonuses for missing modern signals (HTTPS, WhatsApp, JSON-LD,
    analytics, generic H1, template leftovers)
  - Tier-2a: V2 platform scoring with age_score + complexity_score split
  - Tier-2b: modern-stack dampener when 4+ investment signals present
  - Tier-2c: template-leftover detection bonus
  - Returns score + breakdown dict for admin tooltip

run_web_audit_batch dispatches each lead as its own Celery task so all 12
worker processes pull from the queue in parallel — no more sequential loop.

Skips leads that:
  - Have no website URL
  - Are already audited (web_audit_generated_at is set)
  - Have platform shopify or drupal (not our market)
"""
from __future__ import annotations

import time
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

from celery import shared_task

from app.config import get_settings
from app.db.sync_session import sync_session_scope
from app.models import Lead
from app.scrapers.web_audit import (
    audit_website,
    PLATFORM_PITCH_SCORES_V2,
    PLATFORM_PITCH_SCORES,  # kept for backwards-compat with any external callers
    check_url_quality,
    is_generic_h1,
)
from app.utils.pagespeed import fetch_pagespeed
from app.reports.web_audit_report import generate_pdf
from app.utils.logger import get_logger
from app.config import get_settings as _get_settings

log = get_logger(__name__)

# Platforms we don't pitch — skip auditing entirely
SKIP_PLATFORMS = {"shopify", "drupal"}

# How many years old a copyright must be to count as "stale" (pitch signal).
# Threshold rolls forward each year — the same line of code stays correct
# in 2027, 2028, etc.
STALE_COPYRIGHT_THRESHOLD = 3  # current_year - 3

# Tier-2b dampener: if a site shows 4+ modern investment signals, it has
# received recent attention from the owner — drop the pitch by this many
# points so we don't burn outreach on a site that's been refreshed.
MODERN_STACK_DAMPENER = 15
MODERN_STACK_SIGNAL_THRESHOLD = 4  # need this many of: HTTPS, JSON-LD, analytics, WhatsApp, manifest


@dataclass
class PitchScoreBreakdown:
    """Return type for calculate_web_pitch_score() — score + per-component
    explanation. Stored as JSONB on the lead so the admin can see exactly
    why a lead was scored the way it was.
    """
    total: int
    platform: Optional[str]
    platform_age_score: int
    platform_complexity_score: int
    copyright_bonus: int
    mobile_penalty: int
    desktop_penalty: int
    seo_penalty: int
    no_https_bonus: int
    no_whatsapp_bonus: int
    no_jsonld_bonus: int
    no_analytics_bonus: int
    generic_h1_bonus: int
    template_leftover_bonus: int
    modern_stack_signals_count: int
    modern_stack_dampener: int
    capped_at_100: bool

    def to_dict(self) -> dict:
        return self.__dict__.copy()


def calculate_web_pitch_score(
    platform: str | None,
    copyright_year: int | None,
    pagespeed_mobile: int | None,
    pagespeed_desktop: int | None,
    pagespeed_seo: int | None = None,
    *,
    is_https: bool = True,
    has_whatsapp: bool = False,
    has_jsonld: bool = False,
    has_analytics: bool = False,
    has_manifest: bool = False,
    h1_text: Optional[str] = None,
    has_unedited_template: bool = False,
) -> PitchScoreBreakdown:
    """Score 0–100: how strong a web revamp pitch opportunity this lead is.

    V2 components (2026-07-08 audit):

    Platform score (V2 split)         0–30 total
      platform_age_score (0–15):       how old / unsupported the platform is
      platform_complexity_score (0–15): how hard for the owner to migrate
      Modern-stack dampener:           zeroes out age_score when 4+ modern
                                       signals present (recent investment)

    Copyright staleness (tiered)       +10/+20
      age 3–4:                         +10
      age ≥ 5:                         +20
      Threshold rolls forward each year via datetime.now().year.

    PageSpeed penalties                +5 to +25
      mobile  < 50:                    +25
      mobile  50–69:                   +10
      desktop < 50:                    +15
      desktop 50–69:                    +5
      SEO    < 70:                     +15

    Tier-1 signal bonuses              +5 to +10 each
      No HTTPS                         +10
      No WhatsApp on .co.za/.za sites  +10
      No JSON-LD                       +5
      No analytics                     +5
      Generic H1                       +5
      Unedited template                +10

    Modern-stack dampener              -15 if 4+ investment signals

    Returns a PitchScoreBreakdown with `total` for storage and the rest
    for the admin tooltip.
    """
    # -- Platform (V2 split) -------------------------------------------------
    age_score, complexity_score = PLATFORM_PITCH_SCORES_V2.get(platform or "", (0, 0))

    # -- Tier-2b dampener: zero out age_score when the site shows 4+ modern signals
    modern_signals_count = sum([
        bool(is_https),
        bool(has_jsonld),
        bool(has_analytics),
        bool(has_whatsapp),
        bool(has_manifest),
    ])
    dampener = 0
    if modern_signals_count >= MODERN_STACK_SIGNAL_THRESHOLD and age_score > 0:
        # Recent investment → don't punish them for platform age
        dampener = -min(MODERN_STACK_DAMPENER, age_score)
        effective_age_score = max(0, age_score + dampener)
    else:
        effective_age_score = age_score

    score = effective_age_score + complexity_score

    # -- Copyright staleness (tiered, rolls forward) ------------------------
    copyright_bonus = 0
    if copyright_year is not None:
        age = datetime.now().year - copyright_year
        if age >= 5:
            copyright_bonus = 20  # very stale — 5+ years without a redesign
        elif age >= STALE_COPYRIGHT_THRESHOLD:
            copyright_bonus = 10  # noticeably old — 3-4 years

    score += copyright_bonus

    # -- PageSpeed — mobile (weighted higher, mobile-first market) ----------
    mobile_penalty = 0
    if pagespeed_mobile is not None:
        if pagespeed_mobile < 50:
            mobile_penalty = 25
        elif pagespeed_mobile < 70:
            mobile_penalty = 10

    score += mobile_penalty

    # -- PageSpeed — desktop -------------------------------------------------
    desktop_penalty = 0
    if pagespeed_desktop is not None:
        if pagespeed_desktop < 50:
            desktop_penalty = 15
        elif pagespeed_desktop < 70:
            desktop_penalty = 5

    score += desktop_penalty

    # -- SEO score ----------------------------------------------------------
    seo_penalty = 0
    if pagespeed_seo is not None and pagespeed_seo < 70:
        seo_penalty = 15

    score += seo_penalty

    # -- Tier-1 modern-signal bonuses ----------------------------------------
    no_https_bonus = 0 if is_https else 10
    no_whatsapp_bonus = 0 if has_whatsapp else 10
    no_jsonld_bonus = 0 if has_jsonld else 5
    no_analytics_bonus = 0 if has_analytics else 5
    generic_h1_bonus = 5 if is_generic_h1(h1_text) else 0
    template_leftover_bonus = 10 if has_unedited_template else 0

    score += no_https_bonus
    score += no_whatsapp_bonus
    score += no_jsonld_bonus
    score += no_analytics_bonus
    score += generic_h1_bonus
    score += template_leftover_bonus

    # -- Cap -----------------------------------------------------------------
    capped = False
    if score > 100:
        score = 100
        capped = True

    return PitchScoreBreakdown(
        total=score,
        platform=platform,
        platform_age_score=age_score,
        platform_complexity_score=complexity_score,
        copyright_bonus=copyright_bonus,
        mobile_penalty=mobile_penalty,
        desktop_penalty=desktop_penalty,
        seo_penalty=seo_penalty,
        no_https_bonus=no_https_bonus,
        no_whatsapp_bonus=no_whatsapp_bonus,
        no_jsonld_bonus=no_jsonld_bonus,
        no_analytics_bonus=no_analytics_bonus,
        generic_h1_bonus=generic_h1_bonus,
        template_leftover_bonus=template_leftover_bonus,
        modern_stack_signals_count=modern_signals_count,
        modern_stack_dampener=dampener,
        capped_at_100=capped,
    )


def preflight_check(website: str, timeout: float = 6.0) -> dict:
    """Lightweight HEAD request — skip Playwright for network-dead sites.

    Saves ~30 sec of Playwright + ~60 sec of PageSpeed for sites that are
    unreachable at the DNS / TCP / TLS layer. Treats any HTTP response
    (including 4xx/5xx) as "reachable" because Playwright uses a real
    browser and can sometimes bypass simple WAFs where requests cannot.

    Returns dict with keys: ok (bool), status (int), elapsed_ms (int), error (str|None)
    """
    import urllib.request
    import urllib.error

    req = urllib.request.Request(website, method="HEAD", headers={
        "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36 ClientCompassAudit/1.0",
        "Accept": "*/*",
    })
    start = time.monotonic()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return {
                "ok": True,
                "status": resp.status,
                "elapsed_ms": int((time.monotonic() - start) * 1000),
                "error": None,
            }
    except urllib.error.HTTPError as e:
        # 4xx/5xx — got an HTTP response, site is reachable
        return {
            "ok": True,
            "status": e.code,
            "elapsed_ms": int((time.monotonic() - start) * 1000),
            "error": None,
        }
    except Exception as e:
        # Network/SSL/timeout — bail out before Playwright
        reason = getattr(e, "reason", None)
        return {
            "ok": False,
            "status": 0,
            "elapsed_ms": int((time.monotonic() - start) * 1000),
            "error": f"{type(e).__name__}: {reason or e}",
        }


def audit_lead(lead: Lead, api_key: str, screenshot_dir: str) -> dict:
    """Run full web audit for a single lead. Returns dict of field updates."""
    updates: dict = {}

    # -- Pre-flight: skip if site unreachable at network layer -----------
    pf = preflight_check(lead.website)
    if not pf["ok"]:
        log.warning("web_audit_preflight_failed",
                    lead_id=str(lead.id), url=lead.website,
                    elapsed_ms=pf["elapsed_ms"], error=pf["error"])
        updates["web_audit_generated_at"] = datetime.now(timezone.utc)
        updates["web_pitch_score"] = 0  # mark as done so batch query won't re-pick
        updates["audit_error"] = f"preflight: {pf['error']}"
        return updates

    # -- URL quality check: detect booking platforms / social profiles ------
    bad_url, url_issue = check_url_quality(lead.website)
    if bad_url:
        log.info("url_quality_rejected",
                 lead_id=str(lead.id), url=lead.website, reason=url_issue)
        updates["needs_url_review"] = True
        updates["url_quality_issue"] = url_issue

    # -- Playwright audit --------------------------------------------------
    audit = audit_website(lead.website, screenshot_dir=screenshot_dir)

    if audit.error:
        log.warning("web_audit_lead_error", lead_id=str(lead.id), url=lead.website, error=audit.error)
        updates["web_audit_generated_at"] = datetime.now(timezone.utc)
        updates["web_pitch_score"] = 0  # mark as done so batch query won't re-pick
        updates["audit_error"] = f"playwright: {audit.error[:300]}"
        return updates

    if audit.platform in SKIP_PLATFORMS:
        log.info("web_audit_skip_platform", lead_id=str(lead.id), platform=audit.platform)
        updates["website_platform"] = audit.platform
        updates["web_audit_generated_at"] = datetime.now(timezone.utc)
        updates["web_pitch_score"] = 0  # mark as done so batch query won't re-pick
        return updates

    updates["website_platform"] = audit.platform
    updates["site_copyright_year"] = audit.copyright_year
    updates["web_audit_screenshot_path"] = audit.screenshot_path

    # -- V2 modern-web signals (migration 005) ----------------------------
    updates["is_https"] = audit.is_https
    updates["has_whatsapp"] = audit.has_whatsapp
    updates["has_jsonld"] = audit.has_jsonld
    updates["has_analytics"] = audit.has_analytics
    updates["has_manifest"] = audit.has_manifest
    updates["h1_text"] = audit.h1_text
    updates["has_unedited_template"] = audit.has_unedited_template

    # Personalization assets (logo, hero, brand color, gallery)
    updates["scraped_logo_path"]       = audit.logo_path
    updates["scraped_hero_path"]       = audit.hero_path
    updates["scraped_brand_color"]     = audit.brand_color_hex
    # gallery_paths stored as JSON list — keeps schema portable (no JSONB
    # dependency in the ORM layer; the column IS JSONB in Postgres)
    if audit.gallery_paths:
        updates["scraped_gallery_paths"] = json.dumps(audit.gallery_paths)
    elif "scraped_gallery_paths" in updates:
        del updates["scraped_gallery_paths"]

    # Backfill contact info if enrichment missed it
    if audit.phone and not lead.phone:
        updates["phone"] = audit.phone
    if audit.email and not lead.email:
        updates["email"] = audit.email

    # Backfill title as notes hint if empty
    if audit.title and not lead.notes:
        updates["notes"] = f"[web_audit] title: {audit.title[:200]}"

    # -- PageSpeed (mobile + desktop fetched in parallel) ------------------
    ps = fetch_pagespeed(lead.website, api_key=api_key)

    # PageSpeed failure policy: transient timeouts should NOT permanently
    # invalidate a lead. Record the error in audit_error and mark the audit
    # complete with score=0 so the batch skips it next time. The lead stays
    # status='discovered' so it can be manually re-queued if needed.
    if ps.had_failure:
        failed = []
        if ps.mobile_error:  failed.append("mobile")
        if ps.desktop_error: failed.append("desktop")
        log.warning(
            "pagespeed_audit_failed",
            lead_id=str(lead.id), url=lead.website,
            failed_strategies=",".join(failed),
        )
        updates["web_audit_generated_at"] = datetime.now(timezone.utc)
        updates["web_pitch_score"] = 0
        updates["audit_error"] = (
            f"pagespeed_timeout: {','.join(failed) if failed else 'unknown'} (after 60s + retry)"
        )
        return updates

    updates["pagespeed_mobile"]  = ps.mobile_performance
    updates["pagespeed_desktop"] = ps.desktop_performance
    updates["pagespeed_seo"]     = ps.mobile_seo
    updates["pagespeed_a11y"]    = ps.mobile_accessibility

    # -- Pitch score (V2 — full breakdown) ---------------------------------
    breakdown = calculate_web_pitch_score(
        platform=audit.platform,
        copyright_year=audit.copyright_year,
        pagespeed_mobile=updates.get("pagespeed_mobile"),
        pagespeed_desktop=updates.get("pagespeed_desktop"),
        pagespeed_seo=updates.get("pagespeed_seo"),
        is_https=audit.is_https,
        has_whatsapp=audit.has_whatsapp,
        has_jsonld=audit.has_jsonld,
        has_analytics=audit.has_analytics,
        has_manifest=audit.has_manifest,
        h1_text=audit.h1_text,
        has_unedited_template=audit.has_unedited_template,
    )
    updates["web_pitch_score"] = breakdown.total
    # Stash the full breakdown for the admin tooltip. Stored as JSONB.
    updates["pitch_breakdown"] = json.dumps(breakdown.to_dict())

    # -- PDF report --------------------------------------------------------
    try:
        pdf_path = generate_pdf(
            business_name=lead.business_name,
            website=lead.website,
            platform=audit.platform,
            copyright_year=audit.copyright_year,
            pagespeed_mobile=updates.get("pagespeed_mobile"),
            pagespeed_desktop=updates.get("pagespeed_desktop"),
            pagespeed_seo=updates.get("pagespeed_seo"),
            pagespeed_a11y=updates.get("pagespeed_a11y"),
            web_pitch_score=updates["web_pitch_score"],
            screenshot_path=audit.screenshot_path,
            city=getattr(lead, "city", None),
            province=getattr(lead, "province", None),
            phone=updates.get("phone") or getattr(lead, "phone", None),
            email=updates.get("email") or getattr(lead, "email", None),
            google_rating=getattr(lead, "google_rating", None),
            google_review_count=getattr(lead, "google_review_count", None),
            business_type=getattr(lead, "business_type", None),
        )
        updates["web_audit_pdf_path"] = pdf_path
    except Exception as exc:
        log.warning("pdf_generation_failed", lead_id=str(lead.id), error=str(exc))

    updates["web_audit_generated_at"] = datetime.now(timezone.utc)

    log.info(
        "web_audit_lead_done",
        lead_id=str(lead.id),
        url=lead.website,
        platform=audit.platform,
        copyright_year=audit.copyright_year,
        pagespeed_mobile=updates.get("pagespeed_mobile"),
        web_pitch_score=updates["web_pitch_score"],
        pdf_path=updates.get("web_audit_pdf_path"),
        modern_signals=breakdown.modern_stack_signals_count,
        dampener=breakdown.modern_stack_dampener,
    )

    return updates


@shared_task(bind=True, name="app.workers.web_audit.tasks.audit_lead_task")
def audit_lead_task(self, lead_id: str) -> dict:
    """Audit a single lead by ID. Designed to be dispatched individually
    so all 12 Celery workers process leads in parallel from the queue.
    """
    settings = get_settings()
    api_key = settings.google_places_api_key
    screenshot_dir = "/tmp/cc_audits"

    with sync_session_scope() as session:
        lead = session.get(Lead, lead_id)
        if not lead:
            log.warning("audit_lead_task_not_found", lead_id=lead_id)
            return {"status": "not_found"}

    try:
        updates = audit_lead(lead, api_key=api_key, screenshot_dir=screenshot_dir)

        with sync_session_scope() as session:
            db_lead = session.get(Lead, lead.id)
            if db_lead:
                for k, v in updates.items():
                    setattr(db_lead, k, v)
                session.add(db_lead)

        # Queue mockup generation for high-scoring leads.
        # Tier-1 gate (2026-07-08): only queue if the lead has at least one
        # contact channel. Mockups for unreachable leads waste Cloudflare
        # Pages builds. If pitch ≥ threshold but no contact info, set a flag
        # so enrichment can re-trigger after it fills a channel.
        cfg = _get_settings()
        threshold = getattr(cfg, "mockup_pitch_score_threshold", 70)
        pitch_score = updates.get("web_pitch_score", 0) or 0
        if pitch_score >= threshold:
            has_contact = bool(
                getattr(lead, "email", None)
                or getattr(lead, "phone", None)
                or getattr(lead, "whatsapp_number", None)
            )
            if has_contact:
                from app.workers.mockup_generator import generate_mockup
                generate_mockup.delay(str(lead.id))
                log.info(
                    "mockup_queued",
                    lead_id=str(lead.id),
                    pitch_score=pitch_score,
                )
            else:
                # No contact info — defer until enrichment fills a channel.
                with sync_session_scope() as session:
                    db_lead = session.get(Lead, lead.id)
                    if db_lead:
                        db_lead.mockup_eligible_pending_contact = True
                        session.add(db_lead)
                log.info(
                    "mockup_deferred_no_contact",
                    lead_id=str(lead.id),
                    pitch_score=pitch_score,
                )

        return {"status": "ok", "lead_id": lead_id, "pitch_score": pitch_score}

    except Exception as exc:
        log.error("audit_lead_task_exception", lead_id=lead_id, error=str(exc))
        return {"status": "error", "lead_id": lead_id, "error": str(exc)}


@shared_task(bind=True, name="app.workers.web_audit.tasks.run_web_audit_batch")
def run_web_audit_batch(self, batch_size: int = 20) -> dict:
    """Dispatch individual audit_lead_task for each queued lead.

    Each lead becomes its own Celery task so all 12 worker processes pull
    from the queue simultaneously — throughput scales with concurrency.
    The old sequential loop with time.sleep(2) between leads is gone.

    Args:
        batch_size: Max leads to dispatch per beat tick (default 20 from
                    celery_app.py — overridden to 200 via .env/celery_app).
    """
    with sync_session_scope() as session:
        leads = (
            session.query(Lead)
            .filter(Lead.website.isnot(None))
            .filter(Lead.website != "")
            .filter(
                Lead.web_audit_generated_at.is_(None)
                | Lead.web_pitch_score.is_(None)
            )
            .order_by(Lead.discovered_at.asc())
            .limit(batch_size)
            .all()
        )
        lead_ids = [str(lead.id) for lead in leads]

    if not lead_ids:
        log.info("web_audit_queue_empty")
        return {"status": "ok", "dispatched": 0}

    for lead_id in lead_ids:
        audit_lead_task.delay(lead_id)

    log.info("web_audit_batch_dispatched", dispatched=len(lead_ids), batch_size=batch_size)
    return {"status": "ok", "dispatched": len(lead_ids)}