"""Web audit PDF report generator.

Takes a Lead (with web audit fields populated) and produces a branded A4 PDF
using WeasyPrint (HTML→PDF via Cairo/Pango — no headless Chrome needed).

Output path (default): <project>/.cache/reports/<lead_slug>.pdf — durable,
bind-mounted via the .:/app volume in docker-compose.yml. The legacy
/tmp/cc_reports/ location is no longer written to; existing DB rows that
still point at /tmp paths are handled by app.utils.report_assets.resolve
which transparently falls back to the legacy directory on read.
"""
from __future__ import annotations

import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from jinja2 import Environment, FileSystemLoader

from app.utils.logger import get_logger
from app.utils.report_assets import report_dir as _report_dir

log = get_logger(__name__)

_TEMPLATE_DIR = Path(__file__).parent / "templates"

PLATFORM_DISPLAY_NAMES = {
    "wix":                  "Wix",
    "squarespace":          "Squarespace",
    "weebly":               "Weebly",
    "godaddy":              "GoDaddy Website Builder",
    "shopify":              "Shopify",
    "wordpress_divi":       "WordPress + Divi Builder",
    "wordpress_elementor":  "WordPress + Elementor",
    "wordpress_avada":      "WordPress + Avada",
    "wordpress_betheme":    "WordPress + BeTheme",
    "wordpress_generic":    "WordPress",
    "joomla":               "Joomla",
    "drupal":               "Drupal",
    "afrihost_sitebuilder": "Afrihost SiteBuilder",
    "yola":                 "Yola",
    "google_sites":         "Google Sites",
    "static_html":          "Static HTML site",
}

PLATFORM_NOTES = {
    "wix":                  "Wix sites are notoriously slow on mobile and cannot be migrated — a full rebuild is required.",
    "wordpress_divi":       "Divi Builder creates bloated code that significantly slows page load times.",
    "wordpress_elementor":  "Elementor adds heavy JavaScript that hurts mobile performance scores.",
    "joomla":               "Joomla is end-of-mainstream-support and rarely maintained by SA SMEs.",
    "godaddy":              "GoDaddy Website Builder produces slow, template-locked sites with limited SEO control.",
    "static_html":          "This appears to be a hand-coded or very old static site, likely lacking mobile responsiveness.",
    "afrihost_sitebuilder": "Afrihost SiteBuilder produces minimal sites with very limited functionality.",
}


def _score_class(score: int) -> str:
    if score >= 60:
        return "high"
    if score >= 35:
        return "medium"
    return "low"


def _score_headline(score: int) -> str:
    if score >= 60:
        return "High opportunity — significant improvements available"
    if score >= 35:
        return "Moderate opportunity — several areas need attention"
    return "Lower priority — site is reasonably modern"


def _score_description(score: int, platform: str | None, mobile: int | None) -> str:
    parts = []
    if platform and platform not in ("shopify", "drupal"):
        name = PLATFORM_DISPLAY_NAMES.get(platform, platform)
        parts.append(f"Built on {name}")
    if mobile is not None and mobile < 50:
        parts.append(f"mobile performance score of {mobile}/100 (Google threshold: 90+)")
    elif mobile is not None and mobile < 70:
        parts.append(f"mobile performance score of {mobile}/100 — below Google's recommended threshold")
    if not parts:
        return "This website has room for improvement in speed and user experience."
    return "This website has " + ", ".join(parts) + ", which negatively impacts search rankings and customer conversion."


def _speed_class(score: int | None) -> str:
    if score is None:
        return ""
    if score < 50:
        return "bad"
    if score < 70:
        return "warn"
    return "ok"


def _speed_verdict(score: int | None, label: str = "") -> str:
    if score is None:
        return "Could not be measured"
    if score < 50:
        return f"Poor — likely losing customers on {label}" if label else "Poor"
    if score < 70:
        return f"Needs improvement on {label}" if label else "Needs improvement"
    if score < 90:
        return "Acceptable"
    return "Good"


def _build_issues(
    platform: str | None,
    copyright_year: int | None,
    mobile: int | None,
    desktop: int | None,
    seo: int | None,
    a11y: int | None,
) -> list[dict]:
    issues = []
    current_year = datetime.now().year

    if mobile is not None and mobile < 50:
        issues.append({"severity": "", "text": f"Mobile performance score is {mobile}/100 — very slow on smartphones, which make up 70%+ of SA web traffic."})
    elif mobile is not None and mobile < 70:
        issues.append({"severity": "warn", "text": f"Mobile performance score is {mobile}/100 — below Google's recommended threshold of 90."})

    if desktop is not None and desktop < 50:
        issues.append({"severity": "", "text": f"Desktop performance score is {desktop}/100 — slow for all users."})

    if copyright_year is not None and (current_year - copyright_year) >= 3:
        age = current_year - copyright_year
        issues.append({"severity": "warn", "text": f"Copyright footer shows © {copyright_year} — website appears {age} years old without a redesign."})

    if platform in ("wix", "godaddy", "weebly", "afrihost_sitebuilder", "yola"):
        issues.append({"severity": "", "text": f"Built on {PLATFORM_DISPLAY_NAMES.get(platform, platform)} — cannot be optimised; a full rebuild is the only path to a fast, modern site."})

    if seo is not None and seo < 70:
        issues.append({"severity": "", "text": f"SEO score is {seo}/100 — missing key signals that affect Google search rankings."})

    if a11y is not None and a11y < 70:
        issues.append({"severity": "warn", "text": f"Accessibility score is {a11y}/100 — may affect usability for customers with disabilities."})

    return issues


def _slug_from_name(name: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    return slug[:60] or "lead"


def generate_pdf(
    *,
    business_name: str,
    website: str,
    platform: Optional[str] = None,
    copyright_year: Optional[int] = None,
    pagespeed_mobile: Optional[int] = None,
    pagespeed_desktop: Optional[int] = None,
    pagespeed_seo: Optional[int] = None,
    pagespeed_a11y: Optional[int] = None,
    web_pitch_score: int = 0,
    screenshot_path: Optional[str] = None,
    city: Optional[str] = None,
    province: Optional[str] = None,
    phone: Optional[str] = None,
    email: Optional[str] = None,
    google_rating: Optional[float] = None,
    google_review_count: Optional[int] = None,
    business_type: Optional[str] = None,
    output_dir: str = None,
) -> str:
    """Generate a web audit PDF report and return the output file path.

    Default output_dir resolves to <project>/.cache/reports/ via
    report_assets.report_dir(). Callers can override (mostly for tests).
    """
    if output_dir is None:
        output_dir = str(_report_dir())
    Path(output_dir).mkdir(parents=True, exist_ok=True)
    slug = _slug_from_name(business_name)
    output_path = os.path.join(output_dir, f"{slug}.pdf")

    current_year = datetime.now().year
    copyright_age = (current_year - copyright_year) if copyright_year else None
    website_display = re.sub(r'^https?://', '', website).rstrip('/')

    context = {
        "business_name":     business_name,
        "website":           website,
        "website_display":   website_display,
        "generated_date":    datetime.now(timezone.utc).strftime("%d %B %Y"),
        "platform_display":  PLATFORM_DISPLAY_NAMES.get(platform or "", platform or "Unknown"),
        "platform_note":     PLATFORM_NOTES.get(platform or "", ""),
        "copyright_year":    copyright_year,
        "copyright_age":     copyright_age,
        "screenshot_path":   screenshot_path,
        "pagespeed_mobile":  pagespeed_mobile,
        "pagespeed_desktop": pagespeed_desktop,
        "pagespeed_seo":     pagespeed_seo,
        "pagespeed_a11y":    pagespeed_a11y,
        "web_pitch_score":   web_pitch_score,
        "score_class":       _score_class(web_pitch_score),
        "score_headline":    _score_headline(web_pitch_score),
        "score_description": _score_description(web_pitch_score, platform, pagespeed_mobile),
        "mobile_class":      _speed_class(pagespeed_mobile),
        "desktop_class":     _speed_class(pagespeed_desktop),
        "seo_class":         _speed_class(pagespeed_seo),
        "mobile_verdict":    _speed_verdict(pagespeed_mobile, "mobile"),
        "desktop_verdict":   _speed_verdict(pagespeed_desktop, "desktop"),
        "seo_verdict":       _speed_verdict(pagespeed_seo),
        "issues":            _build_issues(platform, copyright_year, pagespeed_mobile, pagespeed_desktop, pagespeed_seo, pagespeed_a11y),
        "city":              city,
        "province":          province,
        "phone":             phone,
        "email":             email,
        "google_rating":     google_rating,
        "google_review_count": google_review_count,
        "business_type":     business_type,
    }

    env = Environment(loader=FileSystemLoader(str(_TEMPLATE_DIR)), autoescape=True)
    template = env.get_template("web_audit_report.html")
    html_str = template.render(**context)

    # Lazy import: Celery Beat never generates PDFs, only schedules this task.
    # The Beat container doesn't have WeasyPrint installed; defer the import
    # to runtime so beat can import this module for task registration without
    # triggering ModuleNotFoundError. The worker container DOES have weasyprint.
    from weasyprint import HTML

    HTML(string=html_str, base_url=str(_TEMPLATE_DIR)).write_pdf(output_path)

    log.info("pdf_generated", business=business_name, path=output_path, score=web_pitch_score)
    return output_path
