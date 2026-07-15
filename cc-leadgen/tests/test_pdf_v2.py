"""Regression tests for the 2026-07-15 PDF + email improvements.

Pins the marketing-tactic refinements so they don't silently regress:

- Email subject mentions "preview + PDF" (not just "preview")
- Email body explicitly mentions the attached PDF
- Email text body has the "attached the full audit report" sentence
- Attachment filename uses `Your-Website-Audit-<Business>.pdf`
- PDF no longer contains "Confidential"
- PDF no longer contains `clientcompass2@gmail.com`
- PDF no longer contains the old "5-7 business days" promise
- PDF contains the prominent copyright-age banner when copyright_year is set
- PDF renders the mockup link + URL when mockup_url is set
- PDF renders a QR code (inline base64 PNG) when mockup_url is set
- PDF avoids the mockup block when mockup_url is None
- `_slugify_for_attachment` produces consistent Title-Case slugs

Run::

    cd ~/installedApps/leadgen/cc-leadgen && source .venv/bin/activate \\
        && pytest -c /dev/null tests/test_pdf_v2.py -v \\
            --no-header -p no:cacheprovider
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


# ── Helpers ──────────────────────────────────────────────────────────────────


class _ShimLead:
    """Minimal stand-in for the SQLAlchemy Lead — only the attributes the
    modern builder reads via getattr() are populated."""

    def __init__(self, **kw):
        for k, v in kw.items():
            setattr(self, k, v)


def _render_email(business_name="Limelight Event Hire", platform="static_html",
                  pagespeed_mobile=41, web_pitch_score=100,
                  mockup_url="https://demo-limelight-event-hire.clientcompass.co.za"):
    """Render the modern web-revamp email end-to-end."""
    from app.utils.email_modern_builder import render_modern_web_revamp
    lead = _ShimLead(
        id="11111111-2222-3333-4444-555555555555",
        business_name=business_name,
        owner_name="Test Recipient",
        email="test@example.com",
        business_type="event planner",
        website_platform=platform,
        pagespeed_mobile=pagespeed_mobile,
        web_pitch_score=web_pitch_score,
        mockup_url=mockup_url,
    )
    return render_modern_web_revamp(
        lead,
        screenshot_ref="data:image/jpeg;base64,/9j/AAAA",
    )


def _render_email_html(lead):
    """Render with the modern builder but skip the screenshot — exercises the
    fallback path used when capture_and_upload_screenshot() returns None."""
    from app.utils.email_modern_builder import render_modern_web_revamp
    return render_modern_web_revamp(lead, screenshot_ref=None)


def _render_pdf(business_name="Limelight Event Hire", platform="static_html",
                copyright_year=2017, pagespeed_mobile=41, web_pitch_score=100,
                mockup_url=None):
    """Render the PDF Jinja2 template to HTML — skips WeasyPrint (slow + needs
    Cairo fonts). All marketing-tactic assertions are over the HTML string."""
    from jinja2 import Environment, FileSystemLoader

    # Try the laptop path first; fall back to the container path.
    candidates = [
        "/home/this0ne/installedApps/leadgen/cc-leadgen/app/reports/templates",
        "/app/app/reports/templates",
    ]
    tmpl_dir = next((p for p in candidates if os.path.isdir(p)), None)
    if tmpl_dir is None:
        raise RuntimeError(
            f"Cannot find web_audit_report.html template — tried {candidates}"
        )

    env = Environment(
        loader=FileSystemLoader(tmpl_dir),
        autoescape=True,
    )
    template = env.get_template("web_audit_report.html")
    ctx = dict(
        business_name=business_name,
        website="http://example.test/",
        website_display="example.test",
        generated_date="15 July 2026",
        platform_display="Static HTML site",
        platform_note="",
        copyright_year=copyright_year,
        copyright_age=(2026 - copyright_year) if copyright_year else None,
        screenshot_path=None,
        pagespeed_mobile=pagespeed_mobile, pagespeed_desktop=53,
        pagespeed_seo=83,        pagespeed_a11y=85,
        web_pitch_score=web_pitch_score,
        score_class="high",      score_headline="H",  score_description="D",
        mobile_class="bad",      desktop_class="bad", seo_class="warn",
        mobile_verdict="v",      desktop_verdict="v", seo_verdict="v",
        issues=[],
        city="Pinetown",         province=None,
        phone="+27 31 700 5832", email=None,
        google_rating=None,      google_review_count=None,
        business_type="event planner",
        mockup_url=mockup_url,
        mockup_qr_data_uri="data:image/png;base64,AAA" if mockup_url else None,
    )
    return template.render(**ctx)


# ── Tests ────────────────────────────────────────────────────────────────────


def test_email_subject_mentions_pdf():
    """Subject was 'preview inside' → now 'preview + PDF inside'."""
    content = _render_email()
    assert "preview + PDF inside" in content.subject, (
        f"Subject should mention 'preview + PDF inside'; got {content.subject!r}"
    )
    # And the old phrasing should be gone from the subject:
    assert not content.subject.startswith("Your website audit for ") or \
           "preview + PDF inside" in content.subject


def test_email_body_html_mentions_attached_pdf():
    """HTML body has a paragraph calling out the attached PDF — the recipient
    needs to know that the paperclip exists."""
    content = _render_email()
    assert "Full audit PDF attached" in content.body_html, (
        "Body HTML should explicitly mention the attached PDF (paperclip)"
    )
    # Plus the safe-template-headline-style phrasing:
    assert "audit PDF" in content.body_html or "PDF attached" in content.body_html


def test_email_body_text_mentions_attached_pdf():
    """Plain-text body is what some clients render by default — must mention
    the PDF explicitly so plain-text readers don't miss it either."""
    content = _render_email()
    assert "attached the full audit report as a PDF" in content.body_text, (
        "Text body should say 'attached the full audit report as a PDF'"
    )


def test_slugify_for_attachment_outreach_module():
    """_slugify_for_attachment in outreach.py produces Title-Case-hyphenated
    slugs that drop non-alpha chars."""
    from app.workers.outreach import _slugify_for_attachment
    cases = [
        ("Limelight Event Hire", "Limelight-Event-Hire"),
        ("DGF Plumbing (Pty) Ltd", "DGF-Plumbing-Pty-Ltd"),
        ("Acme & Co.", "Acme-Co"),
        ("", "lead"),
    ]
    for inp, want in cases:
        got = _slugify_for_attachment(inp)
        assert got == want, f"_slugify_for_attachment({inp!r}) → {got!r}, expected {want!r}"


def test_slugify_for_attachment_email_draft_module():
    """Both modules expose the same helper (slight duplication, but they MUST
    produce identical output — verified here)."""
    from app.workers.email_draft import _slugify_for_attachment as s1
    from app.workers.outreach import _slugify_for_attachment as s2
    for inp in ["Limelight Event Hire", "DGF Plumbing (Pty) Ltd", "Acme & Co.", ""]:
        assert s1(inp) == s2(inp), f"_slugify_for_attachment disagreement: {inp!r}"


def test_pdf_drops_confidential():
    html = _render_pdf()
    assert "Confidential" not in html, (
        "Cover meta should no longer read 'Confidential' — replaced by share-prompt"
    )
    assert "Feel free to forward" in html, (
        "Cover meta should read 'Feel free to forward' instead"
    )


def test_pdf_drops_gmail_footer():
    """Gmail on an enterprise rebuild signals 'side hustle' — use the domain."""
    html = _render_pdf()
    assert "clientcompass2@gmail.com" not in html, (
        "PDF footer should not use the Gmail address"
    )
    assert "info@clientcompass.co.za" in html, (
        "PDF footer should use info@clientcompass.co.za (per operator directive 2026-07-15)"
    )


def test_pdf_softens_5_to_7_day_promise():
    html = _render_pdf()
    assert "5&ndash;7 business days" not in html, (
        "Old '5–7 business days' promise should be replaced (was a hard commitment"
        " written into a 'Confidential' audit PDF)"
    )
    assert "timeline in your proposal" in html or "typically 2" in html, (
        "Timeline promise should now be softer — refer to proposal"
    )


def test_pdf_renders_age_banner_when_copyright_year_set():
    """The 9-years-stale copyright card is the strongest piece of social proof
    — it must be prominently rendered when copyright_year is known."""
    html = _render_pdf(copyright_year=2017)
    assert "age-banner" in html and "9 years" in html, (
        "Prominent copyright-age banner ('9 years') should render when "
        "copyright_year=2017 (current year 2026 → age 9)"
    )


def test_pdf_hides_age_banner_when_copyright_year_missing():
    html = _render_pdf(copyright_year=None)
    # No banner expected when there's no copyright year.
    assert "age-banner-text" not in html or html.count("age-banner") <= 1, (
        "Copyright-age banner should be hidden when copyright_year is None"
    )


def test_pdf_renders_mockup_panel_when_url_set():
    """Mockup URL + QR code should appear when the lead has a live mockup."""
    html = _render_pdf(mockup_url="https://demo-foo.clientcompass.co.za")
    assert "Your free mockup is ready" in html, (
        "Mockup section heading should render"
    )
    assert "demo-foo.clientcompass.co.za" in html, (
        "Mockup URL should be visible (and a clickable link)"
    )
    assert "data:image/png;base64,AAA" in html, (
        "QR code (inline base64 PNG) should be embedded when mockup_url is set"
    )


def test_pdf_hides_mockup_panel_when_url_missing():
    html = _render_pdf(mockup_url=None)
    assert "Your free mockup is ready" not in html, (
        "Mockup section should be hidden when mockup_url is None"
    )
    assert "Scan to view" not in html, (
        "QR code caption should be hidden when no mockup URL"
    )


def test_email_template_key_is_web_revamp_modern():
    """The active path is the modern branded template — Pin so a future
    'simplification' doesn't accidentally fall back to legacy email_builder."""
    content = _render_email()
    assert content.template_key == "web_revamp_modern"


def test_email_has_attachments_dict_with_pdf_path():
    """send_email_draft reads attachments.pdf_path — verify the modern builder
    still populates this so the email_draft → outline → MIME attach flow
    stays compatible after the update."""
    content = _render_email()
    assert content.attachments is not None
    assert "pdf_path" in content.attachments, (
        "attachments dict must carry pdf_path so send_email_draft can resolve "
        "and attach the PDF on send"
    )
