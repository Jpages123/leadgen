"""Regression test: email template footer must not include the physical address.

Background
----------
2026-07-15 operator review of Limelight test email — the footer had a
physical address ("11 Nepeta Street, East-Rural, Kraaifontein, 7570,
South Africa") that the operator wanted removed for prospect-facing
emails. Keeping the company name + email, dropping the street address.

Run with::

    cd ~/installedApps/leadgen/cc-leadgen && source .venv/bin/activate \\
        && pytest -c /dev/null tests/test_email_no_address.py -v \\
            --no-header -p no:cacheprovider
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.utils import email_modern_builder


class FakeLead:
    def __init__(self, **kw):
        for k, v in kw.items():
            setattr(self, k, v)


def test_email_footer_has_no_physical_address():
    """The rendered email must not contain the Kraaifontein street address."""
    lead = FakeLead(
        business_name="Test Business",
        owner_name="",
        email="test@example.com",
        website_platform="wix",
        pagespeed_mobile=41,
        pagespeed_seo=83,
        pagespeed_a11y=52,
        site_copyright_year=2017,
        web_pitch_score=100,
        mockup_url="https://demo.example.com",
        id="test-id",
    )
    content = email_modern_builder.render_modern_web_revamp(lead, None)
    assert "Nepeta" not in content.body_html
    assert "Kraaifontein" not in content.body_html
    assert "East-Rural" not in content.body_html
    assert "7570" not in content.body_html


def test_email_footer_keeps_company_name_and_email():
    """Company name + email stay (only the address line was removed)."""
    lead = FakeLead(
        business_name="Test Business",
        owner_name="",
        email="test@example.com",
        website_platform="wix",
        pagespeed_mobile=41,
        pagespeed_seo=83,
        pagespeed_a11y=52,
        site_copyright_year=2017,
        web_pitch_score=100,
        mockup_url="https://demo.example.com",
        id="test-id",
    )
    content = email_modern_builder.render_modern_web_revamp(lead, None)
    assert "Client Compass Digital Solutions Pty (Ltd)" in content.body_html
    assert "info@clientcompass.co.za" in content.body_html


def test_company_address_constant_removed():
    """The COMPANY_ADDRESS constant must no longer be exported."""
    assert not hasattr(email_modern_builder, "COMPANY_ADDRESS"), (
        "COMPANY_ADDRESS should have been removed from email_modern_builder; "
        "the physical address is no longer included in the email footer"
    )