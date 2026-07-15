"""Smart row picker for the web-revamp email stat card.

Background
----------
The original stat card showed three fixed rows: Mobile, Platform, and
"Opportunity score". The third row was an internal leadgen ranking metric
(``web_pitch_score``) that doesn't communicate anything actionable to a
prospect — it tells them "we sorted you high in our pipeline", which is
self-promotional and opaque. Operator review of the 2026-07-14 Limelight
test email surfaced this as the main thing to fix.

Design (Option A — smart 3-row, agreed 2026-07-15):

  Row 1 — always Mobile performance. The killer metric. Threshold 90.
          Sub-50 = visibly broken on phones.

  Row 2 — Site age, shown only if ``site_copyright_year`` is set.
          Format: ``<N> years old`` (computed from current year). Visceral
          and concrete. 44% of audited leads have a copyright year.

  Row 3 — smart third, picked from the lead's worst pain signal:
          - SEO score, if < 80 (Google threshold 95; sub-80 means
            ranking risk; 4% of audited leads)
          - A11y score, if < 60 (3% of audited leads)
          - Otherwise: Platform ("Built with Wix", "WordPress + Divi",
            etc.) — boring but always informative.

Public API
----------
- ``pick_stat_rows(lead)`` — returns a list of 2 or 3 dicts, each with
  keys ``label``, ``value``, ``color`` (hex) and ``weight`` ("normal" /
  "bold"). Order matches display order.
- ``pick_stat_rows_text(lead)`` — returns the same data as plain-text
  bullets for the text-only email fallback body.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any


# ── Thresholds (mirrors the ones in web_audit_report.py / Phase B spec) ──────
MOBILE_GOOD_THRESHOLD = 90
SEO_GOOD_THRESHOLD = 95
SEO_RISK_THRESHOLD = 70  # below 70% of Google's threshold = real ranking risk
SEO_SHOW_THRESHOLD = 95  # 95+ is acceptable; no point surfacing it
A11Y_CATASTROPHIC = 50  # only flag when truly broken; not a familiar metric


# ── Color palette (matches email_modern_builder.BRAND_*) ─────────────────────
COLOR_RED = "#dc2626"      # bad score
COLOR_AMBER = "#d97706"    # warn
COLOR_GREEN = "#22c55e"    # good / primary brand
COLOR_SLATE_900 = "#0f172a"  # brand text (high-contrast for non-numeric)
COLOR_SLATE_500 = "#475569"  # muted (not used in picker but exported for tests)


def _mobile_color(score: int | None) -> str:
    if score is None:
        return COLOR_SLATE_500
    if score < 50:
        return COLOR_RED
    if score < MOBILE_GOOD_THRESHOLD:
        return COLOR_AMBER
    return COLOR_GREEN


def _seo_color(score: int | None) -> str:
    if score is None:
        return COLOR_SLATE_500
    if score < SEO_RISK_THRESHOLD:
        return COLOR_RED
    if score < SEO_GOOD_THRESHOLD:
        return COLOR_AMBER
    return COLOR_GREEN


def _a11y_color(score: int | None) -> str:
    if score is None:
        return COLOR_SLATE_500
    if score < A11Y_CATASTROPHIC:
        return COLOR_RED
    return COLOR_GREEN


def _site_age(copyright_year: int | None) -> int | None:
    """Years between copyright year and now. ``None`` if unknown."""
    if not copyright_year:
        return None
    try:
        return max(0, datetime.now().year - int(copyright_year))
    except (ValueError, TypeError):
        return None


def _platform_display(platform: str | None) -> str:
    """Best-effort platform display string.

    Mirrors ``PLATFORM_DISPLAY_MAP`` in ``email_builder`` without taking
    an import dependency (this module is imported by both the modern and
    legacy builders, and the email_builder import would create a cycle).
    """
    if not platform:
        return ""
    table = {
        "wix":                 "Wix",
        "squarespace":         "Squarespace",
        "weebly":              "Weebly",
        "godaddy":             "GoDaddy Website Builder",
        "wordpress_divi":      "WordPress + Divi",
        "wordpress_elementor": "WordPress + Elementor",
        "wordpress_avada":     "WordPress + Avada",
        "wordpress_betheme":   "WordPress + BeTheme",
        "wordpress_generic":   "WordPress",
        "joomla":              "Joomla",
        "afrihost_sitebuilder":"Afrihost SiteBuilder",
        "yola":                "Yola",
        "google_sites":        "Google Sites",
        "static_html":         "Static HTML site",
    }
    return table.get(platform, platform)


# ── Public API ───────────────────────────────────────────────────────────────

def pick_stat_rows(lead: Any) -> list[dict]:
    """Return 2 or 3 stat-card rows for the lead.

    Each row is a dict::

        {"label": str, "value": str, "color": "#rrggbb", "weight": "normal"|"bold"}

    Order: Mobile (always) → Site age (if known) → smart third.

    Use the returned list directly in the HTML stat card (one ``<tr>``
    per row) and in the plain-text fallback (one bullet per row).
    """
    rows: list[dict] = []

    # Row 1 — Mobile (always)
    mobile = getattr(lead, "pagespeed_mobile", None)
    mobile_display = f"{mobile} / 100" if mobile is not None else "N/A"
    rows.append({
        "label":  "Mobile performance",
        "value":  mobile_display,
        "color":  _mobile_color(mobile),
        "weight": "bold",
    })

    # Row 2 — Site age (if copyright year known)
    copyright_year = getattr(lead, "site_copyright_year", None)
    age = _site_age(copyright_year)
    if age is not None:
        rows.append({
            "label":  "Site age",
            "value":  f"{age} year{'s' if age != 1 else ''} old",
            "color":  COLOR_SLATE_900,
            "weight": "bold",
        })

    # Row 3 — smart third. SEO wins (Google ranking is concrete and
    # universally understood). Three colour tiers:
    #   < 70  → red "Google ranking risk" (truly bad)
    #   70-94 → amber "SEO score"        (worth flagging, not alarming)
    #   ≥ 95  → omit (acceptable, not worth a row)
    # A11y only flagged when catastrophic (< 50).
    # Platform is the universal fallback — boring but always legible.
    seo = getattr(lead, "pagespeed_seo", None)
    a11y = getattr(lead, "pagespeed_a11y", None)

    if seo is not None and seo < SEO_RISK_THRESHOLD:
        rows.append({
            "label":  "Google ranking risk",
            "value":  f"SEO {seo} / 100",
            "color":  _seo_color(seo),
            "weight": "bold",
        })
    elif seo is not None and seo < SEO_SHOW_THRESHOLD:
        rows.append({
            "label":  "SEO score",
            "value":  f"{seo} / 100",
            "color":  _seo_color(seo),
            "weight": "bold",
        })
    elif a11y is not None and a11y < A11Y_CATASTROPHIC:
        rows.append({
            "label":  "Accessibility",
            "value":  f"{a11y} / 100",
            "color":  _a11y_color(a11y),
            "weight": "bold",
        })
    else:
        # Fallback to platform — boring but always informative
        platform = getattr(lead, "website_platform", None)
        plat_disp = _platform_display(platform)
        if plat_disp:
            rows.append({
                "label":  "Built with",
                "value":  plat_disp,
                "color":  COLOR_SLATE_900,
                "weight": "bold",
            })

    return rows


def pick_stat_rows_text(lead: Any) -> list[str]:
    """Plain-text bullets for the text-only email fallback body."""
    rows = pick_stat_rows(lead)
    out: list[str] = []
    for r in rows:
        out.append(f"- {r['label']}: {r['value']}")
    return out