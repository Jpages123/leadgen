"""Regression test for the vertical-vs-template bug.

Bug history (2026-07-11): when mockup_generator was wired to call the
template analyzer, it overwrote `vertical` with `recommendation["template"]`.
Since VERTICAL_DEFAULTS has no key for 'creative' or 'general',
generate_client_ts silently fell back to _DEFAULT_VERTICAL='trades' — meaning
a PHOTOGRAPHER got:
  - stats labels: 'Jobs Completed', '24/7 Emergency Service'
  - trust badges: 'Registered & Insured', etc.

This test catches that by checking that for a 'photographer' vertical, the
generated client.ts stats/trust badges reflect photography content (not
trades content), AND that the 'creative' defensive alias is present in
VERTICAL_DEFAULTS.
"""
from __future__ import annotations

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from app.utils.client_ts_generator import (  # noqa: E402
    VERTICAL_DEFAULTS,
    generate_client_ts,
)


def test_photographer_vertical_does_not_get_trade_defaults():
    """For a photographer lead, client.ts stats must NOT be trades labels."""
    client_ts = generate_client_ts(
        business_name="Katia Soto Photography",
        tagline="Joburg moments, artfully captured",
        phone="+27825627746",
        email=None,
        address="Johannesburg",
        domain="https://www.katiasotophotography.co.za/",
        city="Johannesburg",
        services=[
            {"title": "Wedding Photography", "description": "Test"},
            {"title": "Portrait Sessions", "description": "Test"},
        ],
        vertical="photography",
        google_rating=5.0,
        google_review_count=25,
        site_copyright_year=2020,
    )
    assert "Shoots Completed" in client_ts, "Photographer stats missing 'Shoots Completed'"
    assert "Emergency Service" not in client_ts, (
        "REGRESSION: photographer client.ts has 'Emergency Service' (trades default leaked)"
    )
    assert "Jobs Completed" not in client_ts, (
        "REGRESSION: photographer client.ts has 'Jobs Completed' (trades default leaked)"
    )


def test_creative_template_aliases_to_photography():
    """Defensive: 'creative' in VERTICAL_DEFAULTS should return photography-style
    content (not trades fallback)."""
    creative_defaults = VERTICAL_DEFAULTS.get("creative")
    assert creative_defaults is not None, (
        "REGRESSION: VERTICAL_DEFAULTS has no 'creative' alias"
    )
    labels = [s["label"] for s in creative_defaults["stats"]]
    assert "Shoots Completed" in labels, (
        f"creative alias has trades-style labels: {labels}"
    )
    assert "Emergency Service" not in labels
    assert "Jobs Completed" not in labels


def test_plumber_keeps_trade_defaults():
    """Sanity: a real plumber (vertical='plumbing') DOES want trades stats."""
    client_ts = generate_client_ts(
        business_name="DGF Plumbing",
        tagline="Local plumbing",
        phone="+27821234567",
        email=None,
        address="Cape Town",
        domain="https://dgfplumbing.co.za/",
        city="Cape Town",
        services=[{"title": "Emergency Repairs", "description": "Test"}],
        vertical="plumbing",
    )
    assert "Jobs Completed" in client_ts, "Plumber should have trade-style stats"
