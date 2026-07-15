"""Regression tests for the smart stat-row picker.

Background
----------
The 2026-07-14 Limelight test email showed a fixed 3-row stat card
including "Opportunity score: 100/100" — an internal leadgen ranking
metric that didn't communicate anything actionable to the prospect. The
operator review surfaced this as the main fix needed; this module
replaces the fixed rows with a smart picker that always shows Mobile +
Site age (if known) + a worst-of third row.

These tests pin the picker's behaviour for all known lead profiles so
future changes to thresholds or platform mapping don't silently regress.

Run with::

    cd ~/installedApps/leadgen/cc-leadgen && source .venv/bin/activate \\
        && pytest -c /dev/null tests/test_email_metrics.py -v \\
            --no-header -p no:cacheprovider
"""
from __future__ import annotations

import os
import sys
from datetime import datetime

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.utils import email_metrics


# ─── Helpers ─────────────────────────────────────────────────────────────────

class FakeLead:
    """Minimal duck-typed stand-in for the SQLAlchemy Lead model."""
    def __init__(self, **kwargs):
        for k, v in kwargs.items():
            setattr(self, k, v)


def _rows(lead):
    """Return list of (label, value) tuples for cleaner assertions."""
    return [(r["label"], r["value"]) for r in email_metrics.pick_stat_rows(lead)]


# ─── Mobile row (always present) ─────────────────────────────────────────────

def test_mobile_row_always_present():
    lead = FakeLead(pagespeed_mobile=41)
    rows = _rows(lead)
    assert rows[0] == ("Mobile performance", "41 / 100")


def test_mobile_row_color_red_when_below_50():
    lead = FakeLead(pagespeed_mobile=41)
    [mobile] = [r for r in email_metrics.pick_stat_rows(lead) if r["label"] == "Mobile performance"]
    assert mobile["color"] == email_metrics.COLOR_RED


def test_mobile_row_color_amber_between_50_and_90():
    lead = FakeLead(pagespeed_mobile=70)
    [mobile] = [r for r in email_metrics.pick_stat_rows(lead) if r["label"] == "Mobile performance"]
    assert mobile["color"] == email_metrics.COLOR_AMBER


def test_mobile_row_color_green_when_above_90():
    lead = FakeLead(pagespeed_mobile=95)
    [mobile] = [r for r in email_metrics.pick_stat_rows(lead) if r["label"] == "Mobile performance"]
    assert mobile["color"] == email_metrics.COLOR_GREEN


def test_mobile_row_handles_none():
    """If pagespeed_mobile is None (audit didn't run), show 'N/A'."""
    lead = FakeLead(pagespeed_mobile=None)
    [mobile] = [r for r in email_metrics.pick_stat_rows(lead) if r["label"] == "Mobile performance"]
    assert mobile["value"] == "N/A"


# ─── Site age row ────────────────────────────────────────────────────────────

def test_site_age_shown_when_copyright_year_known():
    current_year = datetime.now().year
    lead = FakeLead(site_copyright_year=current_year - 5)
    rows = _rows(lead)
    labels = [r[0] for r in rows]
    assert "Site age" in labels
    [age_row] = [r for r in rows if r[0] == "Site age"]
    assert age_row[1] == "5 years old"


def test_site_age_singular_year():
    """1 year old → singular 'year', not 'years'."""
    current_year = datetime.now().year
    lead = FakeLead(site_copyright_year=current_year - 1)
    rows = _rows(lead)
    [age_row] = [r for r in rows if r[0] == "Site age"]
    assert age_row[1] == "1 year old"


def test_site_age_omitted_when_copyright_unknown():
    lead = FakeLead(site_copyright_year=None)
    rows = _rows(lead)
    labels = [r[0] for r in rows]
    assert "Site age" not in labels


# ─── Smart third row ─────────────────────────────────────────────────────────

def test_seo_row_chosen_when_seo_below_threshold():
    """SEO < 85 wins over a11y and platform — Google ranking is concrete."""
    lead = FakeLead(
        pagespeed_mobile=70,
        site_copyright_year=2022,
        pagespeed_seo=72,        # bad
        pagespeed_a11y=52,       # also bad
        website_platform="wordpress_divi",
    )
    rows = _rows(lead)
    [third] = [r for r in rows if r[0] != "Mobile performance" and r[0] != "Site age"]
    assert third[0] == "Google ranking risk"
    assert "SEO 72 / 100" in third[1]


def test_platform_chosen_when_seo_okay_but_a11y_catastrophic():
    """SEO is fine but a11y is catastrophic (< 50) — show accessibility."""
    lead = FakeLead(
        pagespeed_mobile=70,
        site_copyright_year=2022,
        pagespeed_seo=92,        # ok
        pagespeed_a11y=42,       # catastrophic
        website_platform="wordpress_divi",
    )
    rows = _rows(lead)
    [third] = [r for r in rows if r[0] != "Mobile performance" and r[0] != "Site age"]
    assert third[0] == "Accessibility"
    assert third[1] == "42 / 100"


def test_platform_chosen_when_both_seo_and_a11y_okay():
    """If SEO ≥ 85 AND a11y ≥ 50, fall back to platform — universally legible."""
    lead = FakeLead(
        pagespeed_mobile=70,
        site_copyright_year=2022,
        pagespeed_seo=92,
        pagespeed_a11y=88,
        website_platform="wix",
    )
    rows = _rows(lead)
    [third] = [r for r in rows if r[0] != "Mobile performance" and r[0] != "Site age"]
    assert third[0] == "Built with"
    assert third[1] == "Wix"


def test_a11y_in_50_to_60_range_does_not_win():
    """A11y 50-60 should NOT win — too niche a metric for SME owners."""
    lead = FakeLead(
        pagespeed_mobile=70,
        site_copyright_year=2022,
        pagespeed_seo=92,
        pagespeed_a11y=55,       # in the 50-60 range — should NOT win
        website_platform="wix",
    )
    rows = _rows(lead)
    [third] = [r for r in rows if r[0] != "Mobile performance" and r[0] != "Site age"]
    assert third[0] == "Built with"  # platform wins


def test_limelight_specific():
    """The exact Limelight profile: mobile 41, copyright 2017, SEO 83, a11y 52."""
    current_year = datetime.now().year
    expected_age = current_year - 2017
    lead = FakeLead(
        pagespeed_mobile=41,
        site_copyright_year=2017,
        pagespeed_seo=83,
        pagespeed_a11y=52,
        website_platform="static_html",
    )
    rows = _rows(lead)
    assert rows == [
        ("Mobile performance",   "41 / 100"),
        ("Site age",             f"{expected_age} years old"),
        ("Google ranking risk",  "SEO 83 / 100"),
    ]


# ─── Plain-text fallback ─────────────────────────────────────────────────────

def test_pick_stat_rows_text_format():
    lead = FakeLead(pagespeed_mobile=41, site_copyright_year=2017,
                    pagespeed_seo=83, website_platform="static_html")
    text = email_metrics.pick_stat_rows_text(lead)
    assert text[0] == "- Mobile performance: 41 / 100"
    assert any("Site age:" in line for line in text)
    assert any("Google ranking risk:" in line for line in text)


# ─── Platform display mapping ────────────────────────────────────────────────

def test_platform_display_for_known_platforms():
    assert email_metrics._platform_display("wix") == "Wix"
    assert email_metrics._platform_display("wordpress_divi") == "WordPress + Divi"
    assert email_metrics._platform_display("static_html") == "Static HTML site"


def test_platform_display_falls_back_to_raw():
    """Unknown platform returns the raw value rather than crashing."""
    assert email_metrics._platform_display("roblox_site") == "roblox_site"


def test_platform_display_empty_when_none():
    assert email_metrics._platform_display(None) == ""
    assert email_metrics._platform_display("") == ""