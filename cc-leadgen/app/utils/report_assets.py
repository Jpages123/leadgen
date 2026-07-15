"""Durable storage location for generated web-audit PDF reports.

Background
----------
Until 2026-07-15, ``generate_pdf()`` in ``app/reports/web_audit_report.py``
wrote reports to ``/tmp/cc_reports/<slug>.pdf`` — a path guaranteed to
disappear at the next container rebuild, restart, or even ``/tmp`` cleanup.
The DB column ``leads.web_audit_pdf_path`` pointed to those ephemeral paths.
At send time, ``email_draft.send_email_draft`` and
``outreach._build_email_with_pdf`` silently dropped the PDF attachment when
the file was missing — every lead audited more than a few hours before its
email send lost the report. This became visible on 2026-07-14 when
Limelight Event Hire's test send logged ``draft_pdf_attach_failed`` — the
file path was correct in the DB but the file itself no longer existed on
disk.

This module is the single source of truth for where audit PDFs live:

  1. New audits write to ``<project>/.cache/reports/<slug>.pdf`` — durable,
     bind-mounted into the worker container via the existing ``.:/app`` mount,
     gitignored.
  2. ``resolve(slug)`` checks this location first, then falls back to
     ``/tmp/cc_reports/`` for any in-flight pre-fix audits still being
     generated against the legacy path.
  3. ``regenerate_pdf_for_lead(lead)`` is the on-demand recovery path —
     used by the email senders when they find that the DB-referenced path
     is empty/missing. Writes to the durable location; the caller is
     responsible for updating the DB column if it still points at a stale
     path.

The slug convention matches the legacy layout
(``<business-name-slug>.pdf``, derived by ``_slug_from_name`` in
``web_audit_report.py``) so the DB rows that already exist work unchanged
— only the base directory moves.

Public API
----------
- ``report_dir()`` — the durable base directory
- ``report_path(slug)`` — absolute path for a specific PDF
- ``resolve(slug)`` — finds the PDF in either new or legacy location;
  returns ``None`` if missing in both
- ``regenerate_pdf_for_lead(lead)`` — on-demand regen from lead data;
  returns the path to the newly-written PDF, or ``None`` on failure
"""
from __future__ import annotations

import re
from pathlib import Path
from urllib.parse import urlparse

from app.utils.logger import get_logger

log = get_logger(__name__)

# Legacy location — used only as a fallback for resolver lookups against
# pre-fix audits. New writes always go to ``report_dir()``.
_LEGACY_REPORT_DIR = Path("/tmp/cc_reports")


def report_dir() -> Path:
    """Return the durable base directory for web audit PDFs.

    Resolved relative to the project root (parent of ``app/``).
    Created on first call; safe to call repeatedly.

    Bind-mounted into the worker container via the existing ``.:/app``
    volume in ``docker-compose.yml``, so files written here survive
    container restarts and rebuilds.
    """
    project_root = Path(__file__).resolve().parent.parent.parent
    base = project_root / ".cache" / "reports"
    base.mkdir(parents=True, exist_ok=True)
    return base


def report_path(slug: str) -> Path:
    """Return the durable path for a specific PDF.

    Args:
        slug: Business-name-derived slug (e.g. ``limelight-event-hire``).
              Same convention as ``_slug_from_name`` in
              ``app/reports/web_audit_report.py``.

    Returns:
        Absolute path under ``<project>/.cache/reports/<slug>.pdf``.
        File may or may not exist — this is just the expected location.
    """
    if not slug or not slug.strip():
        raise ValueError("slug must be a non-empty string")
    return report_dir() / f"{slug}.pdf"


def resolve(slug: str) -> "Path | None":
    """Find the PDF in the durable location, then the legacy ``/tmp`` location.

    Returns:
        ``Path`` to the PDF if found in either location, else ``None``.
        Callers should treat ``None`` as "needs regen via
        ``regenerate_pdf_for_lead``".
    """
    durable = report_path(slug)
    if durable.exists() and durable.stat().st_size > 0:
        return durable

    legacy = _LEGACY_REPORT_DIR / f"{slug}.pdf"
    if legacy.exists() and legacy.stat().st_size > 0:
        return legacy

    return None


def regenerate_pdf_for_lead(lead, force: bool = False) -> "str | None":
    """Regenerate the web audit PDF for a lead whose file is missing.

    Used by the email senders (``email_draft.send_email_draft`` and
    ``outreach.send_email_sequence``) when ``resolve(slug)`` returns
    ``None``. Writes the new PDF to the durable location so future
    lookups hit the fast path.

    Args:
        lead: A Lead model instance (or duck-typed equivalent) with at
              least ``business_name`` and ``website`` set. Optional fields
              (``pagespeed_*``, ``site_copyright_year``, ``website_platform``,
              ``mockup_url``, etc.) are pulled from the lead if present.
        force: When True, always regenerate even if a non-empty PDF
               already exists at the durable location. Default False
               (fast-path: return existing file). ``send_email_draft``
               passes True to guarantee the attached PDF matches the
               current leadgen data shown in the email body — critical
               when sibling leads share a slug (Limelight has two lead
               rows that both slug to "limelight-event-hire").

    Returns:
        Absolute path to the newly-written PDF, or ``None`` if regeneration
        failed (logged as ``pdf_regen_failed``). On success the file is
        guaranteed to exist and be non-empty, but the DB column
        ``web_audit_pdf_path`` is NOT updated here — the caller is
        responsible for that, since it has the live SQLAlchemy session.
    """
    business_name = getattr(lead, "business_name", None)
    website = getattr(lead, "website", None)
    if not business_name or not website:
        log.warning(
            "pdf_regen_skipped",
            reason="missing business_name or website",
            lead_id=str(getattr(lead, "id", "?")),
        )
        return None

    slug = _slug_from_name(business_name)
    dest = report_path(slug)
    if not force and dest.exists() and dest.stat().st_size > 0:
        # Already exists in durable location — return as-is.
        return str(dest)

    screenshot_path = _resolve_screenshot_for_lead(lead)

    # Lazy import — keeps this module importable in contexts where
    # weasyprint isn't installed (e.g. Celery Beat which never generates
    # PDFs).
    from app.reports.web_audit_report import generate_pdf

    try:
        new_path = generate_pdf(
            business_name=business_name,
            website=website,
            platform=getattr(lead, "website_platform", None),
            copyright_year=getattr(lead, "site_copyright_year", None),
            pagespeed_mobile=getattr(lead, "pagespeed_mobile", None),
            pagespeed_desktop=getattr(lead, "pagespeed_desktop", None),
            pagespeed_seo=getattr(lead, "pagespeed_seo", None),
            pagespeed_a11y=getattr(lead, "pagespeed_a11y", None),
            web_pitch_score=getattr(lead, "web_pitch_score", 0) or 0,
            screenshot_path=screenshot_path,
            city=getattr(lead, "city", None),
            province=getattr(lead, "province", None),
            phone=getattr(lead, "phone", None),
            email=getattr(lead, "email", None),
            google_rating=getattr(lead, "google_rating", None),
            google_review_count=getattr(lead, "google_review_count", None),
            business_type=getattr(lead, "business_type", None),
            output_dir=str(report_dir()),
        )
        log.info(
            "pdf_regen_succeeded",
            lead_id=str(getattr(lead, "id", "?")),
            path=new_path,
        )
        return new_path
    except Exception as exc:
        log.warning(
            "pdf_regen_failed",
            lead_id=str(getattr(lead, "id", "?")),
            error=str(exc),
        )
        return None


# ── helpers ──────────────────────────────────────────────────────────────────

def _slug_from_name(name: str) -> str:
    """Business-name → filesystem slug.

    Mirrors the helper in ``app/reports/web_audit_report.py`` so the two
    locations agree on naming without an import cycle.
    """
    slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    return slug[:60] or "lead"


def _resolve_screenshot_for_lead(lead) -> "str | None":
    """Find the homepage screenshot for the lead's website.

    Mirrors the URL→slug logic in
    ``app/scrapers/web_audit.py:_slug_from_url`` and looks up the asset via
    ``audit_assets.resolve``. Returns ``None`` if the screenshot isn't
    available — the PDF will render without it.
    """
    website = getattr(lead, "website", None)
    if not website:
        return None
    try:
        parsed = urlparse(website)
        slug = parsed.netloc.replace("www.", "").replace(".", "-")
        if not slug:
            return None
        from app.utils.audit_assets import resolve as resolve_asset
        asset = resolve_asset(slug, "screenshot")
        return str(asset) if asset else None
    except Exception:
        return None