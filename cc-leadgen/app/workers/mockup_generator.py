"""Mockup generator Celery task — refactored 2026-07-13.

Previous flow (465 lines, 7 sequential steps, 2 separate Pi subprocess calls):
    copy → analyze → config → clone → build → deploy → approval

New flow: spawn ONE Pi subprocess with the mockup-builder skill loaded.
The LLM drives the pipeline via 9 custom tools, with vision + iteration.
Vision review (mockup_screenshot) is hard-gated in mockup-builder.ts —
mockup_write_approval is blocked until a successful mockup_screenshot call
exists earlier in the session (docs/MOCKUP_VISION_IMPLEMENTATION_PLAN.md).
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import httpx
from datetime import datetime, timezone
from pathlib import Path

from celery import shared_task

from app.config import get_settings
from app.db.sync_session import sync_session_scope
from app.models.lead import Lead
from app.utils.client_ts_generator import slugify
from app.utils.logger import get_logger
from app.utils.pi_slot import pi_slot
from sqlalchemy import select
from app.utils.mockup_build import mockup_project_dir

log = get_logger(__name__)

# Build dir is per-slug, durable — see app/utils/mockup_build.py.
# Use mockup_project_dir(slug) at call sites instead of a constant.
SKILL_PATH = "/app/.pi-docker/agent/extensions/mockup-builder.ts"
PI_BIN = "/root/.npm-global/bin/pi"
DEFAULT_TIMEOUT_S = 600
MAX_ITERATIONS = 3

# Full schema for client.ts and brand.ts injected into every Pi prompt.
# Keeps Pi from having to guess field names or types; eliminates a whole class
# of write_config failures (wrong keys, missing quotes, wrong array formats).
CONFIG_SCHEMA_BLOCK = """
<config_schema>
=== client.ts (export const client) ===
{
  name: string,              // Business name e.g. DGF Plumbing
  tagline: string,           // One-line brand tagline
  phone: string,             // e.g. +27 82 123 4567
  whatsapp: string,          // Digits only, no + e.g. 27821234567
  email: string,             // Contact email
  address: string,           // Short location e.g. Cape Town, Western Cape
  domain: string,            // e.g. dgfplumbing.co.za
  googleMapsEmbed: string,   // Full Google Maps embed URL or 
  services: Array<{
    title: string,
    description: string,
    icon: string,            // MUST be exactly one of: bolt, flame, droplet, home, building,
                              // search, wrench. This is the COMPLETE set implemented in the
                              // template — there is no lucide/icon-font lookup. ANY other value
                              // (including plausible-sounding ones like "camera", "star",
                              // "megaphone", "stage") silently renders the SAME generic wrench
                              // icon on every single card, which is visually broken (identical
                              // icon repeated 3-6x). Pick the closest semantic match from the
                              // 7 above even if imperfect — e.g. "search" for inspection/
                              // consultation services, "building" for structures/venues,
                              // "bolt" for anything technical/electrical/fast-turnaround.
  }>,
  testimonials: Array<{
    name: string,            // Reviewer first name + initial e.g. Dewald M.
    text: string,
    rating: number,          // 1-5
  }>,
  gallery: { src: string, alt: string }[],  // MUST be objects e.g. [{src:"/images/gallery/1.jpg", alt:"..."}]
  web3FormsKey: string,      // Leave as XXXXX — operator fills in later
  cloudflareAnalyticsToken: string,  // Leave as XXXXX
  social: {
    facebook: string | null,
    instagram: string | null,
  },
}

=== brand.ts (export const brand) ===
{
  template: trades | creative | general,
  primaryColor: string,      // Hex e.g. #1a4d5c
  accentColor: string,       // Hex e.g. #f97316
  fontHeading: string,       // Google Font name e.g. Oswald, Playfair Display
  fontBody: string,          // e.g. Inter
  logoPath: string,          // MUST be quoted string: /images/logo.jpg
  heroImage: string,         // MUST be quoted string: /images/hero.jpg
  heroOverlayOpacity: number,  // 0-100 integer
  darkMode: boolean,
  ctaPriority: call | whatsapp | book | contact,
  trustBadges: string[],     // 3-4 short trust signals e.g. [10+ Years Experience]
  imageWatermark: boolean,   // true only for creative template
  galleryTreatment: clean-grid | masonry,
  servicesStyle: icon-cards | minimal-cards | list-style,
}
</config_schema>
"""


@shared_task(bind=True, name="app.workers.mockup_generator.tasks.generate_mockup")
def generate_mockup(self, lead_id: str) -> dict:
    """Spawn a Pi subprocess with the mockup-builder skill to generate the mockup.

    The skill handles the entire pipeline:
      load_lead → (LLM sees prospect's site) → clone template → write config
      → copy assets → build → deploy → verify → iterate (up to 3 times)
      → write approval
    """
    settings = get_settings()

    with sync_session_scope() as session:
        lead = session.get(Lead, lead_id)
        if not lead:
            log.error("mockup_lead_not_found", lead_id=lead_id)
            return {"status": "error", "reason": "lead_not_found"}

        # Tier-1 defensive gate: no contact info → skip
        if not (lead.email or lead.phone or lead.whatsapp_number):
            log.warning(
                "mockup_skipped_no_contact_info",
                lead_id=lead_id,
                email=lead.email, phone=lead.phone, whatsapp=lead.whatsapp_number,
            )
            if lead.mockup_eligible_pending_contact:
                lead.mockup_eligible_pending_contact = False
                session.add(lead)
            return {"status": "skipped", "reason": "no_contact_info"}

        if lead.mockup_status == "generating":
            return {"status": "skipped", "reason": "already_generating"}

        if lead.mockup_status in ("pending_approval", "approved", "rejected"):
            log.info("mockup_skipped_already_processed", lead_id=lead_id, status=lead.mockup_status)
            return {"status": "skipped", "reason": lead.mockup_status}

        # URL quality gate
        if getattr(lead, "needs_url_review", False):
            log.warning(
                "mockup_skipped_url_quality",
                lead_id=lead_id, url=lead.website,
                issue=getattr(lead, "url_quality_issue", None),
            )
            return {"status": "skipped", "reason": "needs_url_review"}

        # Vertical exclusion
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
            )
            return {"status": "skipped", "reason": "excluded_vertical",
                    "vertical": lead.business_type}

        lead.mockup_status = "generating"
        session.add(lead)

    slug = slugify(lead.business_name)
    log.info("mockup_skill_invocation_started", lead_id=lead_id, slug=slug)

    # L2: Run synchronous site snapshot BEFORE spawning Pi, so Pi has factual
    # copy + image data in its context from the very first token.
    from app.workers.mockup_helpers.site_snapshot import site_snapshot, format_snapshot_block
    _snapshot = site_snapshot(lead.website, slug) if lead.website else None
    site_snapshot_block = format_snapshot_block(_snapshot)
    log.info(
        "mockup_site_snapshot_complete",
        lead_id=lead_id,
        snapshot_ok=_snapshot is not None and _snapshot.error is None,
        services_found=len(_snapshot.services) if _snapshot else 0,
        images_ok=(_snapshot.hero is not None and not _snapshot.hero.is_placeholder) if _snapshot else False,
    )

    # Build the prompt — rich context for the LLM (L2: site_snapshot + schema injected)
    prompt = f"""Use the mockup-builder skill to generate a modern mockup landing page for this lead.

{CONFIG_SCHEMA_BLOCK}

{site_snapshot_block}

lead_id: {lead_id}
slug: {slug}
business_name: {lead.business_name}
business_type: {lead.business_type}
city: {lead.city or 'South Africa'}
website: {lead.website or 'N/A'}
website_platform: {lead.website_platform or 'unknown'}
scraped_brand_color: {getattr(lead, 'scraped_brand_color', None) or 'none'}
existing_pitch_score: {lead.web_pitch_score}

Workflow (use the tools in order, max {MAX_ITERATIONS} build iterations):

1. mockup_load_lead(lead_id="{lead_id}") — get full DB context including scraped asset paths.
2. Use Playwright MCP (browser_navigate + browser_take_screenshot) to visit {lead.website or 'their website'}.
   The <site_snapshot> above is your PRIMARY source of truth for copy. Use Playwright only to
   confirm the site's visual aesthetic (colours, imagery style, layout feel) — not to re-extract
   text. If Playwright fails, proceed using only the <site_snapshot> data.
3. Decide template based on business_type AND visual aesthetic from step 2:
   - photography / event_planning → 'creative' (Playfair Display, dark, full-bleed, gallery-first)
   - plumbing / electrical / construction / cleaning / automotive → 'trades' (Oswald, bold, trust signals)
   - everything else → 'general' (balanced)
   Override the default if the existing site's aesthetic strongly suggests another template.
4. mockup_clone_template(template, "{slug}")
5. Write client.ts and brand.ts using the <config_schema> above as your exact field reference.
   COPY RULES — mandatory:
   a) services: use from <site_snapshot> if present. Do NOT invent services not on their site.
   b) phone/email: use from <site_snapshot> if present.
   c) testimonials: use from <site_snapshot> if present; otherwise invent 2 plausible ones.
   d) primaryColor: match scraped_brand_color when available; choose complementary palette otherwise.
   e) logoPath and heroImage MUST be quoted strings e.g. "/images/logo.jpg"
   f) gallery array format is {{src, alt}} OBJECTS — NOT bare strings:
      [
        {{ src: "/images/gallery/1.jpg", alt: "<descriptive alt text>" }},
        {{ src: "/images/gallery/2.jpg", alt: "<descriptive alt text>" }},
        ...
      ]
      Do NOT use bare string paths. Do NOT add hero.jpg, about.jpg as gallery entries.
      Every gallery image MUST be an actual photograph of the business's work, premises,
      staff, product, or events. Do NOT select a scraped asset that is a logo, an icon,
      a generic marketing graphic (e.g. a stock "LIVE STREAMING" badge, a social-media
      icon, an award/accreditation badge), or a screenshot of a webpage/UI.
      NEVER use the exact same image file path more than once in the array — a gallery
      showing the same photo repeated (even cropped differently by the grid) is an
      obvious, embarrassing bug and has shipped before. Count the DISTINCT real photos
      <site_snapshot> actually gives you (check byte sizes / URLs, not just count):
        - 4+ distinct real photos available: use 4, one each, no repeats.
        - 2-3 distinct real photos available: use only that many entries (2 or 3) —
          a slightly shorter gallery looks fine; a repeated photo does not.
        - 0-1 distinct real photos available: use what you have (1 entry, or 0 to hide
          the section if the template supports it) and say so plainly in your rationale
          so the operator knows to source more photos manually — do not manufacture
          duplicates to hit a target count.
   g) hero image strategy: use <site_snapshot> hero local_path if available AND not placeholder.
      If hero is unavailable/placeholder BUT <site_snapshot> has gallery images, use gallery[0].local
      as the hero_path in mockup_copy_assets — a real event photo is always better than a gradient.
      Only fall back to gradient (no hero_path) if no real images exist at all.
   h) Do not write near-duplicate content: no two services with overlapping names/descriptions
      (e.g. "Electrical Maintenance" and "Electrical & Maintenance" describing the same thing —
      merge or differentiate them), and the two "About" paragraphs must each add distinct
      information rather than restating the same sentence in different words.
6. mockup_write_config("{slug}", client_ts, brand_ts)
7. mockup_copy_assets("{slug}", logo_path=..., hero_path=..., gallery_paths=...,
   business_name="{lead.business_name}", accent_color=<chosen>)
   CHECK the returned image_audit block. If hero_is_placeholder=true AND <site_snapshot> contains
   a valid hero_local_path, call mockup_copy_assets again with that corrected hero_path.
8. mockup_build("{slug}")
9. mockup_deploy("{slug}")
10. mockup_verify(<demo_url>, "{lead.business_type or 'general'}")
    If image_audit shows placeholder_count > 0, treat as an issue requiring iteration.
11. MANDATORY VISUAL REVIEW — mockup_write_approval is blocked until this step
    succeeds at least once, so do not skip it: call mockup_screenshot(<demo_url>)
    (desktop viewport at minimum; mobile viewport is strongly recommended given
    prior mobile-overflow bugs) and actually look at the returned image before
    writing anything else. Go through EVERY item below individually and note what
    you actually see for each one — a generic "reviewed, looks clean, no issues"
    is not an acceptable rationale. If you cannot verify an item from the
    screenshot, say so explicitly rather than assuming it's fine:
    - Service card icons: zoom in mentally on the icon inside each service card.
      Are they visually DIFFERENT from each other? If every card shows the exact
      same glyph, you used an icon name outside the 7 supported values (see
      <config_schema> icon field) — go back and fix it to one of the 7 valid names.
    - Section headings ("Our Services", "Gallery", "About", "Reviews") must be
      clearly legible against their background. On dark backgrounds, dark text
      is invisible even if it "should" be styled as a heading — look for this
      specifically since it has appeared before.
    - Gallery and About images are real photographs (people, premises, products,
      events) — not logos, icons, or generic stock/marketing graphics.
    - Gallery images are actually different from each other — if two or more tiles
      look like the exact same photo (even cropped differently), that means you
      duplicated a src path; fix the gallery array per COPY RULE (f) above.
    - Logo placement and cropping — no wide banner logos squeezed/cropped into a
      narrow slot (the exact bug this step exists to catch).
    - Colour palette feels consistent with the lead's brand.
    - Hero image is real (not a grey/gradient placeholder).
    - Layout is not broken — no overlapping text, no illegible contrast, no
      empty/blank rectangles where an image or content should be, no obviously
      unprofessional rendering.
    - Copy matches the lead's actual website — no invented services, no wrong
      phone numbers, no two services or paragraphs that just restate each other.
    You may additionally use Playwright MCP to browse the prospect's original site for
    aesthetic reference, but mockup_screenshot is the tool that satisfies this gate.
12. If issues found (from mockup_verify OR your own visual review in step 11), edit
    config files with read/edit tools, then call mockup_build + mockup_deploy +
    mockup_verify + mockup_screenshot again. Max {MAX_ITERATIONS} total build attempts.
    If you exhaust iterations with an unresolved visual issue, say so plainly in your
    final rationale — do not silently ship a mockup you know looks wrong.
13. When verify passes AND the mockup_screenshot review looks right, OR iterations are
    exhausted: mockup_write_approval(lead_id, mockup_url, recommendation)

Return a final JSON: {{"status": "ok"|"failed", "mockup_url": "...", "iterations": N, "rationale": "..."}}

Important constraints:
- Token discipline: read files with offset/limit. Never read node_modules.
- The build may take 60-90s. Be patient.
- If mockup_build fails, READ THE ERROR carefully before retrying.
- Wrap your final answer in <final>...</final> tags for easy extraction.
"""

    # Acquire a Redis-backed pi subprocess slot before spawning the subprocess.
    # Without this guard, N concurrent generate_mockup tasks would all race for
    # pi's internal lock and most would hit the subprocess 300s timeout waiting.
    # The slot pool guarantees only  (default 1) pi processes
    # run simultaneously; the rest queue in Redis via BLPOP and release on exit.
    slot_timeout = get_settings().pi_slot_timeout_s

    try:
        with pi_slot(timeout=slot_timeout):
            proc = subprocess.run(
                [PI_BIN, "-p", prompt, "--extension", SKILL_PATH, "--no-skills"],
                capture_output=True, text=True, timeout=DEFAULT_TIMEOUT_S,
                env={
                    **os.environ,
                    "PATH": f"/root/.npm-global/bin:{os.environ.get('PATH', '')}",
                    "MOCKUP_PROJECT_ROOT": "/app",
                    "MOCKUP_PYTHON_BIN": "/app/.venv/bin/python",
                },
                cwd="/app",
            )

        if proc.returncode != 0:
            log.error("mockup_pi_subprocess_failed",
                       lead_id=lead_id, returncode=proc.returncode,
                       stderr=proc.stderr[-500:])
            _mark_failed(lead_id)
            return {"status": "error", "reason": "pi_subprocess_failed",
                    "stderr": proc.stderr[-300:]}

        # Extract final answer from pi output
        final = _extract_final_answer(proc.stdout)
        log.info("mockup_pi_subprocess_complete",
                  lead_id=lead_id,
                  output_chars=len(proc.stdout),
                  final_present=final is not None)

        # Find the mockup_url from the skill output. If the LLM wrote an
        # approval row in admin_crm.mockup_approvals, the prod DB has the URL.
        # Otherwise, parse it from the LLM's final summary JSON.
        mockup_url = _extract_mockup_url(final, proc.stdout)

        if not mockup_url:
            log.warning("mockup_no_url_found", lead_id=lead_id,
                         final_summary=final[:500])
            _mark_failed(lead_id)
            return {"status": "error", "reason": "no_mockup_url_in_output",
                    "summary": final[:500]}

        # HARDENING: The LLM sometimes hallucinates success — claims it
        # deployed the mockup without actually calling the deploy tools
        # (mockup_pages_live / mockup_dns_live / mockup_write_approval),
        # then reports a phantom URL. The worker used to trust that claim
        # and flip the lead to pending_approval, leaving a row in leadgen
        # DB with no real Cloudflare Pages project behind it.
        #
        # Now we verify the URL returns a real Astro site before trusting
        # the LLM. If verification fails (DNS NXDOMAIN, HTTP non-200, body
        # < 5KB after a brief settle, business-name not in HTML) we treat
        # the build as failed and trigger the immediate retry.
        with sync_session_scope() as session:
            lead_for_verify = session.get(Lead, lead_id)
            business_name = lead_for_verify.business_name if lead_for_verify else None

        verified = _verify_mockup_url(mockup_url, business_name=business_name)
        if not verified:
            log.warning(
                "mockup_url_verification_failed",
                lead_id=lead_id,
                url=mockup_url,
                business_name=business_name,
            )
            _mark_failed(lead_id)
            return {
                "status": "error",
                "reason": "mockup_url_unreachable",
                "url": mockup_url,
            }

        log.info("mockup_url_verified", lead_id=lead_id, url=mockup_url)

        # The LLM is the source of truth for the mockup. It already wrote the
        # approval row in prod admin DB via mockup_write_approval. We just
        # need to finalize the leadgen DB state.
        with sync_session_scope() as session:
            lead_after = session.get(Lead, lead_id)
            if lead_after:
                lead_after.mockup_status = "pending_approval"
                lead_after.mockup_url = mockup_url
                lead_after.mockup_generated_at = datetime.now(timezone.utc)
                session.add(lead_after)

        log.info("mockup_pending_approval", lead_id=lead_id,
                  url=mockup_url, final_summary=final[:200])
        return {"status": "ok", "mockup_url": mockup_url,
                "lead_id": lead_id, "summary": final}

    except TimeoutError as exc:
        # Raised by pi_slot() when no slot became available within slot_timeout.
        # Distinct from subprocess.TimeoutExpired (the subprocess itself hanging).
        log.error("mockup_pi_slot_timeout", lead_id=lead_id, slot_timeout=slot_timeout)
        _mark_failed(lead_id)
        return {"status": "error", "reason": "pi_slot_timeout"}
    except subprocess.TimeoutExpired as exc:
        partial = (exc.stdout or "")[-1000:] if hasattr(exc, "stdout") else ""
        log.error("mockup_pi_timeout", lead_id=lead_id, timeout=DEFAULT_TIMEOUT_S,
                  partial_stdout=partial)
        _mark_failed(lead_id)
        return {"status": "error", "reason": "pi_timeout"}
    except Exception as exc:
        log.error("mockup_skill_invocation_failed", lead_id=lead_id, error=str(exc))
        _mark_failed(lead_id)
        raise


def _extract_mockup_url(final: str | None, raw: str) -> str | None:
    """Find the mockup URL from the LLM's output.

    Looks for:
      1. JSON {"mockup_url": "..."} in the final summary
      2. demo-<slug>.clientcompass.co.za pattern anywhere in raw output
    """
    if final:
        try:
            data = json.loads(final)
            if isinstance(data, dict) and data.get("mockup_url"):
                return data["mockup_url"]
        except (json.JSONDecodeError, ValueError):
            pass
    # Fallback: regex search in raw output
    match = re.search(r"https://demo-[a-z0-9-]+\.clientcompass\.co\.za", raw)
    if match:
        return match.group(0)
    return None


# Minimum body size for a real Astro mockup. The base template renders to
# ~30 KB even with stub content; a sub-5 KB response means we got the
# Cloudflare error page, a 0-byte CDN edge cache, or the LLM lied about
# deploying.
MOCKUP_MIN_BYTES = 5000
# Some businesses have names that won't appear verbatim in the rendered
# HTML (legal suffixes, capitalisation drift). We log a warning rather
# than failing the build on a name mismatch.
VERIFY_TIMEOUT_S = 20
VERIFY_RETRIES = 2  # brief retries for Cloudflare edge cache settle
VERIFY_RETRY_DELAY_S = 4


def _verify_mockup_url(url: str, business_name: str | None = None) -> bool:
    """Verify the deployed mockup actually returns a real Astro site.

    Returns True only if:
      - HTTP 200 (final response after redirects)
      - body length >= MOCKUP_MIN_BYTES (not an error page / CDN miss)
      - business_name (if provided) appears in the HTML (warn-only)

    False on any DNS error, timeout, non-200, or undersized body.

    Retries up to VERIFY_RETRIES times with VERIFY_RETRY_DELAY_S between
    attempts to ride out Cloudflare edge cache settling after a fresh
    Pages deploy.
    """
    import time as _t
    last_err: str | None = None
    last_status: int | None = None
    last_size: int | None = None

    for attempt in range(1, VERIFY_RETRIES + 1):
        try:
            r = httpx.get(
                url,
                timeout=VERIFY_TIMEOUT_S,
                follow_redirects=True,
                headers={"User-Agent": "Mozilla/5.0 (compatible; cc-mockup-verify)"},
            )
            last_status = r.status_code
            last_size = len(r.content)
            if r.status_code == 200 and last_size >= MOCKUP_MIN_BYTES:
                if business_name and business_name.lower() not in r.text.lower():
                    log.warning(
                        "mockup_verify_name_mismatch",
                        url=url,
                        expected=business_name,
                        status=r.status_code,
                        size=last_size,
                    )
                    # Warn-only — pass anyway, since some business names
                    # render with subtle diffs (Pty Ltd, capitalisation).
                return True
            last_err = f"status={r.status_code} size={last_size}"
        except httpx.HTTPError as exc:
            last_err = f"{type(exc).__name__}: {exc}"

        if attempt < VERIFY_RETRIES:
            log.info(
                "mockup_verify_retry",
                url=url,
                attempt=attempt,
                max=VERIFY_RETRIES,
                last_err=last_err,
                last_status=last_status,
                last_size=last_size,
            )
            _t.sleep(VERIFY_RETRY_DELAY_S)

    log.warning(
        "mockup_verify_exhausted",
        url=url,
        attempts=VERIFY_RETRIES,
        last_status=last_status,
        last_size=last_size,
        last_err=last_err,
    )
    return False


def _mark_failed(lead_id: str) -> None:
    """Mark a lead as failed and schedule an immediate retry (Phase M+).

    Updates leads.mockup_status to 'failed', then schedules
    retry_single_mockup to run after mockup_auto_retry_delay_s so the
    lead gets another shot without waiting for the 30-min beat sweep.

    The retry task itself enforces the mockup_max_auto_retries cap, so
    this function stays simple. Failure modes covered:
      - pi_slot_timeout (legacy 300s slot wait — fixed by pi_slot v2)
      - pi_timeout (subprocess timeout — Pi hung for DEFAULT_TIMEOUT_S)
      - mockup_subprocess_failed (pi returned non-zero)
      - mockup_no_url_found (LLM output didn't contain mockup_url)
      - any other exception in the skill pipeline
    """
    with sync_session_scope() as session:
        lead = session.get(Lead, lead_id)
        if lead:
            lead.mockup_status = "failed"
            session.add(lead)

    # Schedule immediate retry. Import lazily — the regen module imports
    # generate_mockup at module load, which would create a circular import
    # since mockup_generator.py is imported by mockup_regen.py.
    from app.workers.mockup_regen import retry_single_mockup
    delay = get_settings().mockup_auto_retry_delay_s
    retry_single_mockup.apply_async(args=[lead_id], countdown=delay)
    log.info("mockup_auto_retry_scheduled", lead_id=lead_id, delay_s=delay)


def _extract_final_answer(stdout: str) -> str | None:
    """Pull the LLM's final <final>...</final> tagged answer from pi output."""
    match = re.search(r"<final>(.*?)</final>", stdout, re.DOTALL)
    if match:
        return match.group(1).strip()

    # Fallback: last 1500 chars (most likely contains the summary)
    return stdout[-1500:].strip() if stdout else None
