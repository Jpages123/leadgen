"""Mockup generator Celery task — refactored 2026-07-13.

Previous flow (465 lines, 7 sequential steps, 2 separate Pi subprocess calls):
    copy → analyze → config → clone → build → deploy → approval

New flow: spawn ONE Pi subprocess with the mockup-builder skill loaded.
The LLM drives the pipeline via 8 custom tools, with vision + iteration.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path

from celery import shared_task

from app.config import get_settings
from app.db.sync_session import sync_session_scope
from app.models.lead import Lead
from app.utils.client_ts_generator import slugify
from app.utils.logger import get_logger
from sqlalchemy import select

log = get_logger(__name__)

MOCKUP_BUILD_DIR = Path("/tmp/cc_mockups")
SKILL_PATH = "/app/.pi-docker/agent/extensions/mockup-builder.ts"
PI_BIN = "/root/.npm-global/bin/pi"
DEFAULT_TIMEOUT_S = 300
MAX_ITERATIONS = 3


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

    # Build the prompt — concise context for the LLM
    prompt = f"""Use the mockup-builder skill to generate a modern mockup landing page for this lead:

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

1. mockup_load_lead(lead_id="{lead_id}") — get full context including scraped assets
2. Use Playwright MCP to navigate to {lead.website or 'their website'} and screenshot it. Note the prospect's current visual style.
3. Decide template based on business_type:
   - photography / event_planning → 'creative' (Playfair Display, dark, full-bleed, gallery-first)
   - plumbing / electrical / construction / cleaning / automotive → 'trades' (Oswald, bold, trust signals)
   - everything else → 'general' (balanced)
   Override if the existing site's aesthetic strongly suggests another template.
4. mockup_clone_template(template, "{slug}")
5. Write client.ts and brand.ts content. Match the scraped brand color when available.
   CRITICAL: client.ts `logo` field MUST be a quoted string like `"/images/logo.jpg"` (Session 10 regression).
6. mockup_write_config("{slug}", client_ts, brand_ts)
7. mockup_copy_assets("{slug}", logo_path=..., hero_path=..., gallery_paths=..., business_name="{lead.business_name}", accent_color=<chosen>)
8. mockup_build("{slug}")
9. mockup_deploy("{slug}")
10. mockup_verify(<demo_url>, "{lead.business_type or 'general'}")
11. ALSO use Playwright MCP to screenshot the live mockup and look at it. Compare to the prospect's site.
12. If issues found, edit the config files with read/edit tools, then call mockup_build + mockup_deploy + mockup_verify again. Max {MAX_ITERATIONS} build attempts.
13. When verify passes OR you've used all iterations: mockup_write_approval(lead_id, mockup_url, recommendation)

Return a final JSON: {{"status": "ok"|"failed", "mockup_url": "...", "iterations": N, "rationale": "..."}}

Important constraints:
- Token discipline: read files with offset/limit. Never read node_modules.
- The build may take 60-90s. Be patient.
- If mockup_build fails, READ THE ERROR, then decide whether to retry or ship.
- If Playwright MCP fails (e.g. browser_navigate errors), fall back to mockup_verify (which is programmatic only).
- Wrap your final answer in <final>...</final> tags for easy extraction.
"""

    try:
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

    except subprocess.TimeoutExpired:
        log.error("mockup_pi_timeout", lead_id=lead_id, timeout=DEFAULT_TIMEOUT_S)
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


def _mark_failed(lead_id: str) -> None:
    with sync_session_scope() as session:
        lead = session.get(Lead, lead_id)
        if lead:
            lead.mockup_status = "failed"
            session.add(lead)


def _extract_final_answer(stdout: str) -> str | None:
    """Pull the LLM's final <final>...</final> tagged answer from pi output."""
    match = re.search(r"<final>(.*?)</final>", stdout, re.DOTALL)
    if match:
        return match.group(1).strip()

    # Fallback: last 1500 chars (most likely contains the summary)
    return stdout[-1500:].strip() if stdout else None
