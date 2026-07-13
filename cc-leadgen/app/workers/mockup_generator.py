"""Celery task: generate a personalised mockup site for a high-scoring lead.

Full pipeline:
  1. Generate copy (tagline + service descriptions) via Pi LLM harness
  2. Generate client.ts + brand.ts from scraped data + copy
  3. Clone cc-site-template, inject config files, download images
  4. pnpm build
  5. Deploy to Cloudflare Pages + create DNS A record
  6. Write approval record to admin_crm.mockup_approvals on prod DB (via PROD_DB_URL)
  7. Update lead: mockup_url, mockup_status=pending_approval

Approval is given via the admin portal at login.clientcompass.co.za/admin/mockup-approvals.
Outreach worker will NOT send the mockup email until mockup_status=approved.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import uuid
from datetime import datetime, timezone
from pathlib import Path

from celery import shared_task

from app.config import get_settings
from app.db.sync_session import sync_session_scope
from app.models import Lead
from app.utils.client_ts_generator import generate_client_ts, generate_brand_ts, generate_brand_ts_for_recommendation, slugify
from app.utils.cloudflare_deploy import deploy_mockup
from app.utils.copy_generator import generate_copy
from app.utils.template_analyzer import analyze_site_for_mockup
from app.utils.logger import get_logger

log = get_logger(__name__)

import os as _os
_GITHUB_TOKEN = _os.environ.get("GITHUB_TOKEN", "")
TEMPLATE_REPO = (
    f"https://{_GITHUB_TOKEN}@github.com/Jpages123/cc-site-template.git"
    if _GITHUB_TOKEN
    else "git@github.com:Jpages123/cc-site-template.git"
)
MOCKUP_BUILD_DIR = Path(tempfile.gettempdir()) / "cc_mockups"

_VERTICAL_KEYWORDS: dict[str, list[str]] = {
    "plumbing": ["plumb", "geyser", "pipe", "drain", "sanit"],
    "electrical": ["electr", "solar", "wiring", "distribution board"],
    "beauty": ["hair", "salon", "beauty", "nail", "lash", "barber", "spa"],
    "cleaning": ["clean", "maid", "housekeep", "sanitiz", "hygiene"],
    "photography": ["photo", "videograph", "wedding shoot", "portrait"],
    "automotive": ["car wash", "detailing", "valet", "tyre", "mechanic", "automo"],
}


def _safe_json_list(value) -> list[str]:
    """Decode a JSON string to a list, or pass through if already a list.
    Used to read the scraped_gallery_paths column (stored as JSONB).
    """
    if value is None:
        return []
    if isinstance(value, list):
        return value
    if isinstance(value, str):
        try:
            parsed = _json.loads(value)
            return parsed if isinstance(parsed, list) else []
        except Exception:
            return []
    return []


def _detect_vertical(lead: Lead) -> str:
    text = " ".join(filter(None, [
        lead.business_type,
        lead.business_name,
        getattr(lead, "notes", None),
    ])).lower()
    for vertical, keywords in _VERTICAL_KEYWORDS.items():
        if any(kw in text for kw in keywords):
            return vertical
    return "trades"


def _resolve_pnpm() -> str:
    found = shutil.which("pnpm")
    if found:
        return found
    for candidate in [
        Path.home() / ".nvm/versions/node/v23.1.0/bin/pnpm",
        Path("/usr/local/bin/pnpm"),
    ]:
        if candidate.exists():
            return str(candidate)
    raise FileNotFoundError("Cannot locate pnpm")


def _clone_template(dest_dir: Path) -> None:
    if dest_dir.exists():
        shutil.rmtree(dest_dir)
    result = subprocess.run(
        ["git", "clone", "--depth=1", TEMPLATE_REPO, str(dest_dir)],
        capture_output=True, text=True, timeout=60,
    )
    if result.returncode != 0:
        raise RuntimeError(f"git clone failed: {result.stderr[:300]}")


def _pnpm_build(project_dir: Path) -> None:
    # Remove stale dist/ so prerender uses freshly compiled output
    stale_dist = project_dir / "dist"
    if stale_dist.exists():
        shutil.rmtree(stale_dist)
    pnpm = _resolve_pnpm()
    env = os.environ.copy()
    env["PATH"] = f"{Path(pnpm).parent}:{env.get('PATH', '')}"
    for cmd in [["install", "--frozen-lockfile"], ["build"]]:
        result = subprocess.run(
            [pnpm] + cmd,
            capture_output=True, text=True,
            cwd=str(project_dir),
            env=env,
            timeout=120,
        )
        if result.returncode != 0:
            raise RuntimeError(
                f"pnpm {cmd[0]} failed (exit {result.returncode}): {result.stderr[-400:]}"
            )


def _write_approval_to_prod_db(
    lead_id: str,
    business_name: str,
    website: str | None,
    mockup_url: str,
    vertical: str,
    web_pitch_score: int | None,
    pagespeed_mobile: int | None,
    website_platform: str | None,
    city: str | None,
    email: str | None,
    lead_type: str = "bad_website",
) -> None:
    """Insert a pending approval record into admin_crm.mockup_approvals on prod DB."""
    settings = get_settings()
    prod_db_url = settings.prod_db_url
    if not prod_db_url:
        log.warning("prod_db_url_not_set — skipping approval record write")
        return

    import psycopg2
    from urllib.parse import urlparse

    parsed = urlparse(prod_db_url.replace("+asyncpg", ""))
    # Parse sslmode from query string (e.g. ?sslmode=disable)
    from urllib.parse import parse_qs
    qs = parse_qs(parsed.query)
    sslmode = qs.get("sslmode", ["prefer"])[0]

    conn = psycopg2.connect(
        host=parsed.hostname,
        port=parsed.port or 5432,
        dbname=parsed.path.lstrip("/").split("?")[0],
        user=parsed.username,
        password=parsed.password,
        connect_timeout=10,
        sslmode=sslmode,
    )
    try:
        with conn:
            with conn.cursor() as cur:
                # Set admin context so RLS allows the insert
                cur.execute("SET LOCAL app.is_admin = 'true'")
                cur.execute(
                    """
                    INSERT INTO admin_crm.mockup_approvals
                      (id, lead_id, business_name, website, mockup_url, vertical,
                       web_pitch_score, pagespeed_mobile, website_platform, city, email,
                       lead_type, status, created_at, updated_at)
                    VALUES
                      (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 'pending', NOW(), NOW())
                    ON CONFLICT (lead_id) DO UPDATE SET
                      mockup_url = EXCLUDED.mockup_url,
                      status = 'pending',
                      actioned_at = NULL,
                      updated_at = NOW()
                    """,
                    (
                        str(uuid.uuid4()),
                        lead_id,
                        business_name,
                        website,
                        mockup_url,
                        vertical,
                        web_pitch_score,
                        pagespeed_mobile,
                        website_platform,
                        city,
                        email,
                        lead_type,
                    ),
                )
        log.info("approval_record_written", lead_id=lead_id, mockup_url=mockup_url)
    finally:
        conn.close()


@shared_task(bind=True, name="app.workers.mockup_generator.tasks.generate_mockup")
def generate_mockup(self, lead_id: str) -> dict:
    """Generate, deploy, and create an admin approval record for a mockup site."""
    settings = get_settings()

    with sync_session_scope() as session:
        lead = session.get(Lead, lead_id)
        if not lead:
            log.error("mockup_lead_not_found", lead_id=lead_id)
            return {"status": "error", "reason": "lead_not_found"}

        # ── Tier-1 defensive gate (2026-07-08) ─────────────────────────────
        # Don't deploy a mockup for a lead we have no way to contact.
        # web_audit should never queue us with no-contact leads (it sets
        # mockup_eligible_pending_contact instead), but defence in depth:
        # if we're called anyway (manual re-trigger, race, etc.), bail out.
        if not (lead.email or lead.phone or lead.whatsapp_number):
            log.warning(
                "mockup_skipped_no_contact_info",
                lead_id=lead_id,
                email=lead.email, phone=lead.phone, whatsapp=lead.whatsapp_number,
            )
            # Clear the pending-contact flag if it was set, so we don't
            # loop forever — let enrichment re-trigger when contact arrives.
            if lead.mockup_eligible_pending_contact:
                lead.mockup_eligible_pending_contact = False
                session.add(lead)
            return {"status": "skipped", "reason": "no_contact_info"}

        # Already in progress by another task dispatch
        if lead.mockup_status == "generating":
            return {"status": "skipped", "reason": "already_generating"}

        # Skip leads already past the generation gate
        if lead.mockup_status in ("pending_approval", "approved", "rejected"):
            log.info("mockup_skipped_already_processed", lead_id=lead_id, status=lead.mockup_status)
            return {"status": "skipped", "reason": lead.mockup_status}

        # Skip leads whose URL was flagged as low-quality (booking platform,
        # social profile, or directory). The lead still has an audit + score,
        # but the URL is not the business's own site, so we can't extract
        # brand assets from it. Skip mockup, set status back to 'none' so it
        # can be re-tried once the URL is corrected in the DB.
        if getattr(lead, "needs_url_review", False):
            log.warning(
                "mockup_skipped_url_quality",
                lead_id=lead_id, url=lead.website,
                issue=getattr(lead, "url_quality_issue", None),
            )
            return {
                "status": "skipped",
                "reason": "needs_url_review",
                "issue": getattr(lead, "url_quality_issue", None),
            }

        # Skip excluded verticals (salons / hair / nail / beauty).
        # These verticals need a booking system — out of scope for web revamp.
        # Added 2026-07-11.
        import re
        _EXCLUDED_VERTICAL_RE = re.compile(r"hair|nail|beauty|barber|salon|nails", re.IGNORECASE)
        _EXCLUDED_TYPES = {"hair salon", "nail salon", "beauty salon", "barber shop", "beauty"}
        if (
            _EXCLUDED_VERTICAL_RE.search(lead.business_name or "")
            or (lead.business_type or "").lower() in _EXCLUDED_TYPES
        ):
            log.warning(
                "mockup_skipped_excluded_vertical",
                lead_id=lead_id,
                business_type=lead.business_type,
                business_name=lead.business_name,
            )
            return {
                "status": "skipped",
                "reason": "excluded_vertical",
                "vertical": lead.business_type,
            }

        lead.mockup_status = "generating"
        session.add(lead)
        snap = {
            "id": str(lead.id),
            "business_name": lead.business_name,
            "phone": lead.phone,
            "email": lead.email,
            "website": lead.website,
            "city": lead.city,
            "business_type": lead.business_type,
            "website_platform": lead.website_platform,
            "pagespeed_mobile": lead.pagespeed_mobile,
            "web_pitch_score": lead.web_pitch_score,
            "web_audit_screenshot_path": lead.web_audit_screenshot_path,
            "facebook_url": lead.facebook_url,
            "instagram_url": lead.instagram_url,
            "google_rating": float(lead.google_rating) if lead.google_rating else None,
            "google_review_count": lead.google_review_count,
            # Personalization assets (added 2026-07-04)
            "scraped_logo_path":       getattr(lead, "scraped_logo_path", None),
            "scraped_hero_path":       getattr(lead, "scraped_hero_path", None),
            "scraped_brand_color":     getattr(lead, "scraped_brand_color", None),
            "scraped_gallery_paths":   _safe_json_list(getattr(lead, "scraped_gallery_paths", None)),
        }

    vertical = _detect_vertical(lead)
    slug = slugify(snap["business_name"])
    project_dir = MOCKUP_BUILD_DIR / slug

    log.info("mockup_generation_started", lead_id=lead_id, slug=slug, vertical=vertical)

    try:
        # ── Step 1: LLM copy ─────────────────────────────────────────────────
        copy = generate_copy(
            business_name=snap["business_name"],
            vertical=vertical,
            city=snap["city"],
            timeout=getattr(settings, "mockup_pi_timeout", 60),
        )

        # ── Step 1b: Pi analyzes site for template + visual treatment ─────
        recommendation = analyze_site_for_mockup(
            business_name=snap["business_name"],
            vertical=vertical,
            city=snap["city"],
            website=snap["website"],
            website_platform=snap["website_platform"],
            scraped_brand_color=snap["scraped_brand_color"],
            scraped_gallery_paths=snap["scraped_gallery_paths"],
            pagespeed_mobile=snap["pagespeed_mobile"],
            site_copyright_year=getattr(lead, "site_copyright_year", None) if lead else None,
            google_rating=snap["google_rating"],
            google_review_count=snap["google_review_count"],
            timeout=getattr(settings, "mockup_pi_timeout", 60),
        )
        # Update vertical to chosen template (used downstream for fallback decisions)
        template = recommendation["template"]
        log.info(
            "mockup_template_recommended",
            lead_id=lead_id,
            template=template,
            vertical=vertical,
            rationale=recommendation["rationale"][:160],
        )

        # ── Step 2: Generate config files ────────────────────────────────────
        client_ts = generate_client_ts(
            business_name=snap["business_name"],
            tagline=copy["tagline"],
            phone=snap["phone"],
            email=snap["email"],
            address=None,
            domain=snap["website"] or "",
            city=snap["city"],
            services=copy["services"],
            vertical=vertical,
            google_rating=snap["google_rating"],
            google_review_count=snap["google_review_count"],
            facebook_url=snap["facebook_url"],
            instagram_url=snap["instagram_url"],
            # Personalization
            logo_path=snap["scraped_logo_path"],
            hero_image_path=snap["scraped_hero_path"],
            gallery_paths=snap["scraped_gallery_paths"] or [],
        )
        brand_ts = generate_brand_ts_for_recommendation(
            recommendation=recommendation,
            vertical=vertical,
            logo_path="/images/logo.jpg",
            hero_image_path="/images/hero.jpg",
        )

        # ── Step 3: Clone + inject ───────────────────────────────────────────
        MOCKUP_BUILD_DIR.mkdir(parents=True, exist_ok=True)
        _clone_template(project_dir)
        (project_dir / "src" / "config" / "client.ts").write_text(client_ts)
        (project_dir / "src" / "config" / "brand.ts").write_text(brand_ts)

        # ── Personalization assets: copy scraped images into project ────────
        # Ensure target dirs exist (template may not ship public/images/)
        images_dir = project_dir / "public" / "images"
        gallery_dir = images_dir / "gallery"
        images_dir.mkdir(parents=True, exist_ok=True)

        # Logo — always copy as /images/logo.jpg to match template's
        # brand.logoPath and hardcoded Nav/Footer references
        logo_src = snap.get("scraped_logo_path")
        if logo_src and Path(logo_src).exists():
            shutil.copy(logo_src, images_dir / "logo.jpg")
            log.info("mockup_logo_copied", src=logo_src, lead_id=lead_id)
        # Hero image — always copy as /images/hero.jpg
        hero_src = snap.get("scraped_hero_path")
        if hero_src and Path(hero_src).exists():
            shutil.copy(hero_src, images_dir / "hero.jpg")
            log.info("mockup_hero_copied", src=hero_src, lead_id=lead_id)
        elif snap.get("web_audit_screenshot_path") and Path(snap["web_audit_screenshot_path"]).exists():
            # Fallback: use the audit screenshot (it's the OLD ugly site, but
            # better than nothing when hero extraction failed)
            shutil.copy(
                snap["web_audit_screenshot_path"],
                images_dir / "hero.jpg",
            )
            log.info("mockup_hero_fallback_to_screenshot", lead_id=lead_id)
        # Gallery images (only create gallery dir if we actually have any)
        gallery_paths = snap.get("scraped_gallery_paths") or []
        if gallery_paths:
            gallery_dir.mkdir(parents=True, exist_ok=True)
            for i, g_path in enumerate(gallery_paths[:4], 1):
                if g_path and Path(g_path).exists():
                    ext = Path(g_path).suffix or ".jpg"
                    shutil.copy(g_path, gallery_dir / f"{i}{ext}")
                    log.info("mockup_gallery_copied", idx=i, src=g_path, lead_id=lead_id)

        # ── Step 4: Build ────────────────────────────────────────────────────
        _pnpm_build(project_dir)

        # ── Step 5: Deploy ───────────────────────────────────────────────────
        mockup_url = deploy_mockup(slug=slug, dist_dir=str(project_dir / "dist"))
        log.info("mockup_deployed", lead_id=lead_id, url=mockup_url)

        # ── Step 6: Write approval record to prod DB ─────────────────────────
        _write_approval_to_prod_db(
            lead_id=snap["id"],
            business_name=snap["business_name"],
            website=snap["website"],
            mockup_url=mockup_url,
            vertical=vertical,
            web_pitch_score=snap["web_pitch_score"],
            pagespeed_mobile=snap["pagespeed_mobile"],
            website_platform=snap["website_platform"],
            city=snap["city"],
            email=snap["email"],
        )

        # ── Step 7: Update lead ──────────────────────────────────────────────
        with sync_session_scope() as session:
            db_lead = session.get(Lead, lead_id)
            if db_lead:
                db_lead.mockup_url = mockup_url
                db_lead.mockup_status = "pending_approval"
                db_lead.mockup_generated_at = datetime.now(timezone.utc)
                session.add(db_lead)

        try:
            shutil.rmtree(project_dir)
        except Exception:
            pass

        log.info("mockup_pending_approval", lead_id=lead_id, mockup_url=mockup_url)
        return {"status": "ok", "mockup_url": mockup_url, "slug": slug}

    except Exception as exc:
        log.error("mockup_generation_failed", lead_id=lead_id, error=str(exc))
        with sync_session_scope() as session:
            db_lead = session.get(Lead, lead_id)
            if db_lead:
                db_lead.mockup_status = "failed"
                session.add(db_lead)
        shutil.rmtree(project_dir, ignore_errors=True)
        raise
