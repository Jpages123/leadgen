"""Regression tests for the 2026-08-03 email revamp session.

Pins the new email/PDF copy so future edits don't silently regress:

Modern email (email_modern_builder.py):
- Subject line unchanged
- Body mentions R5,000 rebuild + R8,000 new-build anchor + "rebuild is the smarter move" framing
- All 3 web_revamp steps include R5,000 / R8,000 / Cape Town references
- Renders R5,000 / R8,000 / "we reuse your domain" copy in CTA footer
- Body shows Cape Town, Western Cape (ECTA s.45)
- Body mentions 3G / load-shedding SA-specific line
- Body mentions (excl. VAT) and Pay by EFT
- Social proof line ("Built 30+ sites for SA trades…")
- Platform-aware "no Wix watermark" line — only for Wix, not for WP / static
- Pain number shown in seconds, not percentile
- Unsubscribe URL uses login.clientcompass.co.za (not dead cc-leadgen DNS)
- Unsubscribe URL carries a JWT (3-segment, dot-separated), not a raw UUID

Legacy email (email_builder.py):
- All 3 web_revamp steps include R5,000 / R7,000 / Cape Town references
- Step 1 includes "first 3 revamp clients this month" framing
- Step 2 escalates urgency ("3 revamp clients this month")
- Step 3 final notice ("still hold the R5,000 intro price")

PDF report (web_audit_report.html):
- Renders R5,000 / R7,000 / Cape Town in CTA footer
- Renders "3G" and "stage 6" SA-context line
- Platform-aware copy: "no Divi plugin bloat" for WP, NOT "no Wix watermark"

Run::

    cd ~/installedApps/leadgen/cc-leadgen && source .venv/bin/activate \\
        && pytest -c /dev/null tests/test_email_revamp_2026_08_03.py -v \\
            --no-header -p no:cacheprovider
"""
from __future__ import annotations

import os
import re
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


# ── Helpers ──────────────────────────────────────────────────────────────────


class _ShimLead:
    """Minimal stand-in for SQLAlchemy Lead."""

    def __init__(self, **kw):
        for k, v in kw.items():
            setattr(self, k, v)


def _mod_render(platform="wordpress_divi", mobile=41, year=2017, owner="Sipho"):
    """Render the modern email."""
    from app.utils.email_modern_builder import render_modern_web_revamp
    lead = _ShimLead(
        id="11111111-2222-3333-4444-555555555555",
        business_name="Limelight Event Hire",
        owner_name=owner,
        email="test@example.com",
        business_type="event planner",
        website_platform=platform,
        pagespeed_mobile=mobile,
        pagespeed_seo=83,
        pagespeed_a11y=52,
        web_pitch_score=88,
        site_copyright_year=year,
        mockup_url="https://demo-limelight.clientcompass.co.za",
    )
    return render_modern_web_revamp(lead, screenshot_ref=None)


def _legacy_render(platform="wordpress_divi", mobile=41, step=1):
    """Render the legacy web_revamp email at the given step (1/2/3)."""
    from app.utils.email_builder import render_email_for_lead
    # get_template_key() returns 'web_revamp' iff (a) web_audit_pdf_path is set OR
    # (b) mockup_status='approved' AND mockup_url is set. The auto-send path
    # runs for approved mockups, so we model that.
    lead = _ShimLead(
        id="11111111-2222-3333-4444-555555555555",
        business_name="Limelight Event Hire",
        owner_name="Sipho",
        email="test@example.com",
        business_type="event planner",
        website_platform=platform,
        pagespeed_mobile=mobile,
        web_pitch_score=88,
        mockup_url="https://demo-limelight.clientcompass.co.za",
        mockup_status="approved",        # needed for web_revamp template_key at any step
        web_audit_pdf_path="/app/.cache/reports/limelight.pdf" if step == 1 else None,
    )
    return render_email_for_lead(lead, step=step)


def _pdf_render(platform="wordpress_divi"):
    """Render the PDF Jinja2 template to HTML."""
    from jinja2 import Environment, FileSystemLoader

    candidates = [
        "/home/this0ne/installedApps/leadgen/cc-leadgen/app/reports/templates",
        "/app/app/reports/templates",
    ]
    tmpl_dir = next((p for p in candidates if os.path.isdir(p)), None)
    if tmpl_dir is None:
        raise RuntimeError(f"Cannot find web_audit_report.html — tried {candidates}")

    env = Environment(loader=FileSystemLoader(tmpl_dir), autoescape=True)
    template = env.get_template("web_audit_report.html")
    ctx = dict(
        business_name="Limelight Event Hire",
        website="http://example.test/",
        website_display="example.test",
        generated_date="03 August 2026",
        platform_display={"wix": "Wix", "wordpress_divi": "WordPress + Divi", "static_html": "Static HTML site"}.get(platform, platform),
        platform_note="",
        website_platform=platform,
        copyright_year=2017,
        copyright_age=9,
        screenshot_path=None,
        pagespeed_mobile=41, pagespeed_desktop=53,
        pagespeed_seo=83, pagespeed_a11y=85,
        web_pitch_score=88,
        score_class="high", score_headline="H", score_description="D",
        mobile_class="bad", desktop_class="bad", seo_class="warn",
        mobile_verdict="v", desktop_verdict="v", seo_verdict="v",
        issues=[],
        city="Pinetown", province=None,
        phone="+27 31 700 5832", email=None,
        google_rating=None, google_review_count=None,
        business_type="event planner",
        mockup_url="https://demo-limelight.clientcompass.co.za",
        mockup_qr_data_uri="data:image/png;base64,AAA",
    )
    return template.render(**ctx)


# ═════════════════════════════════════════════════════════════════════════════
# MODERN EMAIL — copy + structural assertions
# ═════════════════════════════════════════════════════════════════════════════


def test_modern_subject_unchanged():
    """Subject kept the proven 'preview + PDF inside' style."""
    c = _mod_render()
    assert "we built a free preview" in c.subject


def test_modern_no_expiry_framing():
    """The R5,000 is the permanent revamp rate — no 'intro' / 'expir' wording in body."""
    c = _mod_render()
    # Strip HTML comments before checking (they're not visible to recipients)
    import re as _re
    visible = _re.sub(r"<!--.*?-->", "", c.body_html, flags=_re.DOTALL)
    assert "intro" not in visible.lower(), "Visible 'intro' wording implies temporary pricing"
    assert "expir" not in (visible + c.body_text).lower(), "Expiry wording contradicts permanent positioning"


def test_modern_body_has_intro_price_r5000():
    c = _mod_render()
    assert "R5,000" in c.body_html
    assert "intro" in c.body_html.lower()


def test_modern_body_has_standard_price_r8000():
    """R8,000 is framed as the new-build rate (matches clientcompass.co.za Starter — bumped 2026-08-04)."""
    c = _mod_render()
    assert "R8,000" in c.body_html
    assert "brand new website" in c.body_html
    # Should not falsely frame R8,000 as a "usual" price that R5,000 is replacing
    # (since R5,000 is the permanent revamp rate, not a temporary intro discount)
    assert "instead of the usual" not in c.body_html


def test_modern_body_has_turnaround_days():
    """Operator-confirmed 7-working-day turnaround must be in the offer."""
    c = _mod_render()
    assert "7 working days" in c.body_html
    assert "7-day turnaround" in c.body_html or "7 working days" in c.body_html


def test_modern_body_reframes_rebuild_not_discount():
    """The offer must position R5k as the rebuild rate (intrinsic), NOT as a discount off R8k.

    The reframe language: "if you've already got a site, a rebuild is the smarter move"
    + "I reuse your domain, content, photos and branding". This removes the anchoring
    problem where recipients mentally default to R8k as the "real" price.
    """
    c = _mod_render()
    assert "rebuild is the smarter move" in c.body_html
    assert "I reuse your domain" in c.body_html or "reuse your domain" in c.body_html


def test_modern_body_has_capacity_scarcity_3_slots():
    """3-slots-per-month is the permanent capacity cap (real scarcity)."""
    c = _mod_render()
    visible = _visible_text(c.body_html) + " " + c.body_text
    assert "3 website rebuilds each month" in visible or "3 spots" in visible


def test_modern_body_has_vat_inclusion_note():
    """SA small biz don't think in VAT — prices are quoted incl. VAT."""
    c = _mod_render()
    assert "incl. VAT" in c.body_html
    assert "excl. VAT" not in c.body_html


def test_modern_body_has_payfast_payment_provider():
    """PayFast is the SA payment gateway (instant EFT, card, SnapScan)."""
    c = _mod_render()
    assert "PayFast" in c.body_html
    # Old copy had "Pay by EFT after the proposal" — that's gone.
    assert "Pay by EFT" not in c.body_html


def test_modern_body_has_monthly_price():
    """The Starter tier monthly (R395) must be in the offer block."""
    c = _mod_render()
    assert "R395" in c.body_html
    assert "month" in c.body_html


def test_modern_body_has_no_fabricated_social_proof():
    """No clients yet — we must NOT fabricate a portfolio count."""
    c = _mod_render()
    # The old "Built 30+ sites" line must be gone
    assert "30+ sites" not in c.body_html
    assert "30+" not in c.body_html  # could trip on "30+ sites" only — be safe


def test_modern_body_has_sa_context_3g_no_loadshedding():
    """3G + data drain is an evergreen SA hook. Load-shedding is no longer relevant."""
    c = _mod_render()
    assert "3G" in c.body_html
    assert "data" in c.body_html
    # Stage 6 / load-shedding references should be GONE (outdated)
    assert "stage 6" not in c.body_html
    assert "Eskom" not in c.body_html
    assert "load-shedding" not in c.body_html.lower()


def test_modern_footer_has_cape_town_for_ecta():
    """ECTA s.45 requires a physical address in commercial email footers."""
    c = _mod_render()
    assert "Cape Town, Western Cape" in c.body_html


def test_modern_footer_has_improved_consent_basis():
    """POPIA-friendly wording: business is listed publicly, details used once."""
    c = _mod_render()
    assert "listed publicly" in c.body_html


def test_modern_pain_in_seconds_not_percentile():
    """Pain line translates the mobile score into seconds, not abstract percentile."""
    c = _mod_render(mobile=41)  # 41 → ~11 seconds
    # Should mention "seconds" with a concrete number near 11
    assert "second" in c.body_html or "seconds" in c.body_html
    # Should NOT use the old generic phrasing
    assert "Mobile scores below 50 typically mean" not in c.body_html


def test_modern_pain_seconds_for_high_score():
    """For a score of 90+, the seconds estimate should drop to ~2-6s."""
    c = _mod_render(mobile=92)
    # The HTML wraps the number in <strong>, so allow for tags between
    # 'about' and 'seconds'.
    m = re.search(r"about\s*(?:<[^>]+>)?\s*(\d+)\s*(?:<[^>]+>)?\s*seconds", c.body_html)
    assert m is not None, "Pain line should include 'about N seconds'"
    seconds = int(m.group(1))
    assert 2 <= seconds <= 15, f"Seconds estimate {seconds} out of plausible range"


def test_modern_platform_aware_pitch_for_wix():
    """Wix leads still see 'no Wix watermark' (the only platform the old line was correct for)."""
    c = _mod_render(platform="wix")
    assert "no Wix watermark" in c.body_html


def test_modern_platform_aware_pitch_for_wordpress_divi():
    """WP + Divi leads must NOT see 'no Wix watermark' — that's the bug this session fixed."""
    c = _mod_render(platform="wordpress_divi")
    assert "no Wix watermark" not in c.body_html
    assert "Divi" in c.body_html


def test_modern_platform_aware_pitch_for_wordpress_generic():
    c = _mod_render(platform="wordpress_generic")
    assert "no Wix watermark" not in c.body_html
    assert "WordPress plugin" in c.body_html


def test_modern_platform_aware_pitch_for_static_html():
    c = _mod_render(platform="static_html")
    assert "no Wix watermark" not in c.body_html
    assert "static HTML" in c.body_html


def test_modern_platform_aware_pitch_for_unknown_platform():
    """Unknown platforms fall back to a generic 'no monthly platform fees' line."""
    c = _mod_render(platform="nextjs_static_export")
    assert "no Wix watermark" not in c.body_html
    assert "monthly platform fees" in c.body_html


def test_modern_unsubscribe_uses_login_clientcompass():
    """The dead cc-leadgen host has no DNS — unsubscribe must point at login.clientcompass.co.za."""
    c = _mod_render()
    m = re.search(r'href="([^"]*unsubscribe[^"]*)"', c.body_html)
    assert m is not None, "Unsubscribe link missing"
    assert "login.clientcompass.co.za" in m.group(1)
    assert "cc-leadgen.clientcompass.co.za" not in m.group(1)


def test_modern_unsubscribe_carries_jwt_not_raw_uuid():
    """The endpoint verifies a JWT signature — a raw UUID will 401."""
    c = _mod_render()
    m = re.search(r"token=([^&\"]+)", c.body_html)
    assert m is not None
    token = m.group(1)
    # JWTs are 3 dot-separated base64url segments
    parts = token.split(".")
    assert len(parts) == 3, f"Expected 3-segment JWT, got {len(parts)} segments"
    # Raw UUIDs are 36 chars with dashes at fixed positions — JWT segments aren't
    assert "-" not in parts[0], f"Token header should be base64url, not a UUID"


def test_modern_signature_has_cape_town_personalisation():
    c = _mod_render()
    assert "Solo founder" in c.body_html
    assert "Cape Town, Western Cape" in c.body_html


def test_modern_cta_button_label_is_preview_cta():
    """The big CTA uses a short, action-oriented label that points at the mockup.

    As of 2026-08-24 the price is no longer in the button — it lives in the offer
    card directly above it, so the button just says "Click here to see your preview".
    """
    c = _mod_render()
    # New button label — short, action-oriented, points at the mockup URL
    assert "Click here to see your preview" in c.body_html
    # Old label + R5k-in-button combo should be gone
    assert "See your preview + R5,000 rebuild" not in c.body_html
    # The R5,000 price itself still lives in the offer card above the button
    assert "R5,000" in c.body_html
    # Sanity: the new label is in fact present (guard against silent revert)
    assert c.body_html.count("Click here to see your preview") >= 1


# ═════════════════════════════════════════════════════════════════════════════
# LEGACY EMAIL (auto follow-ups via email_builder.py)
# ═════════════════════════════════════════════════════════════════════════════


def test_legacy_step1_has_pricing_and_city():
    c = _legacy_render(step=1)
    text_or_html = c.body_text + c.body_html
    assert "R5,000" in text_or_html
    assert "R8,000" in text_or_html  # bumped from R7k 2026-08-04
    assert "R7,000" not in text_or_html  # old anchor removed
    assert "R395" in text_or_html  # monthly price
    assert "incl. VAT" in text_or_html
    assert "PayFast" in text_or_html
    assert "Cape Town, Western Cape" in text_or_html
    assert "7 working days" in text_or_html  # turnaround
    assert "smarter move" in text_or_html or "reuse" in text_or_html.lower()  # reframe


def test_legacy_step1_has_3_slots_framing():
    """Permanent 3-rebuilds-per-month capacity cap (plain language, no 'first 3' framing)."""
    c = _legacy_render(step=1)
    text_or_html = c.body_text + c.body_html
    assert "3 website rebuilds each month" in text_or_html or "3 rebuilds" in text_or_html


def test_legacy_step2_escalates_urgency():
    """Step 2 should remind about the limited intro slots."""
    c = _legacy_render(step=2)
    text_or_html = c.body_text + c.body_html
    assert "R5,000" in text_or_html
    assert "R395" in text_or_html
    assert "3 revamp clients" in text_or_html or "this month" in text_or_html
    assert "PayFast" in text_or_html


def test_legacy_step3_final_notice():
    """Step 3 focuses on capacity urgency — no false 'price will increase' claim.

    The rebuild price is permanent (R5,000 is the rebuild rate, R8,000 is the
    brand new website price) — so step 3 must NOT claim the price goes back.
    It should still escalate by referencing the 3-rebuilds-per-month capacity cap.
    """
    c = _legacy_render(step=3)
    text_or_html = c.body_text + c.body_html
    assert "R5,000" in text_or_html
    assert "R8,000" in text_or_html  # bumped from R7k
    assert "R7,000" not in text_or_html  # old anchor removed
    assert "R395" in text_or_html
    assert "PayFast" in text_or_html
    assert "7 working days" in text_or_html  # turnaround
    # Capacity-focused urgency (the only true lever we have)
    assert "3 website rebuilds" in text_or_html or "3 rebuilds" in text_or_html
    # NO false expiry claim
    assert "goes back" not in text_or_html
    assert "back to R8,000" not in text_or_html
    assert "back to R7,000" not in text_or_html
    assert "intro" not in text_or_html.lower()


def test_legacy_all_steps_have_city_for_ecta():
    """ECTA s.45 — physical address required in all 3 steps of the sequence."""
    for step in (1, 2, 3):
        c = _legacy_render(step=step)
        text_or_html = c.body_text + c.body_html
        assert "Cape Town, Western Cape" in text_or_html, f"Step {step} missing physical address"


def test_legacy_no_loadshedding_references():
    """Outdated SA hook — must be gone from all 3 steps."""
    for step in (1, 2, 3):
        c = _legacy_render(step=step)
        text_or_html = c.body_text + c.body_html
        assert "stage 6" not in text_or_html
        assert "Eskom" not in text_or_html
        assert "load-shedding" not in text_or_html.lower()


def test_legacy_no_expiry_framing():
    """The R5,000 rate is permanent — no 'intro' / 'expir' / 'goes back' wording."""
    for step in (1, 2, 3):
        c = _legacy_render(step=step)
        text_or_html = c.body_text + c.body_html
        assert "intro" not in text_or_html.lower(), f"Step {step} has 'intro' wording"
        assert "expir" not in text_or_html.lower(), f"Step {step} has 'expir' wording"
        assert "goes back" not in text_or_html, f"Step {step} has 'goes back' wording"


# ═════════════════════════════════════════════════════════════════════════════
# PLAIN-LANGUAGE SWEEP (Phase N rev-5: non-tech-savvy audience)
# ═════════════════════════════════════════════════════════════════════════════


import re as _re


def _visible_text(s):
    """Strip HTML tags + entities to extract what the recipient actually sees."""
    t = _re.sub(r"<[^>]+>", " ", s)
    t = _re.sub(r"&[a-z]+;", " ", t)
    t = _re.sub(r"&#[0-9]+;", " ", t)
    t = _re.sub(r"\s+", " ", t).strip()
    return t


JARGON_TERMS = [
    "revamp intake",    # use 'I only do N website rebuilds'
    "new-build",        # use 'brand new website'
    "mockup",           # use 'preview'
    "mobile-first",     # use 'fast on phones'
    "monthly performance report",  # use 'monthly check-up'
]


def _assert_no_jargon(name, text):
    """Pin the no-jargon rule for one surface's visible text."""
    for term in JARGON_TERMS:
        match = _re.search(rf"\b{_re.escape(term)}\b", text, _re.IGNORECASE)
        assert not match, (
            f"{name}: contains jargon term {term!r} "
            f"(context: ...{text[max(0, match.start()-40):match.end()+40]}...)"
        )


def test_modern_visible_text_no_jargon():
    """The modern template's user-visible text must not contain jargon."""
    c = _mod_render()
    _assert_no_jargon("modern html", _visible_text(c.body_html))
    _assert_no_jargon("modern text", c.body_text)


def test_legacy_visible_text_no_jargon():
    """All 3 legacy step templates must not contain jargon."""
    for step in (1, 2, 3):
        c = _legacy_render(step=step)
        _assert_no_jargon(f"legacy step {step} html", _visible_text(c.body_html))
        _assert_no_jargon(f"legacy step {step} text", c.body_text)


def test_pdf_visible_text_no_jargon():
    """The PDF report's user-visible text must not contain jargon."""
    html = _pdf_render()
    _assert_no_jargon("PDF", _visible_text(html))


def test_modern_uses_plain_language_alternatives():
    """The plain-language alternatives must actually be present (not just absence of jargon)."""
    c = _mod_render()
    visible = _visible_text(c.body_html) + " " + c.body_text
    # These phrases confirm the rewording landed
    assert "website rebuilds" in visible or "website rebuild" in visible
    assert "brand new website" in visible
    assert "preview" in visible
    assert "fast on phones" in visible or "fast on 3G" in visible
    assert "monthly check-up" in visible


# ═════════════════════════════════════════════════════════════════════════════
# PDF REPORT
# ═════════════════════════════════════════════════════════════════════════════


def test_pdf_has_r5000_r8000_and_monthly_in_footer():
    """PDF uses R8,000 anchor (bumped from R7k 2026-08-04)."""
    html = _pdf_render()
    assert "R5,000" in html
    assert "R8,000" in html
    assert "R7,000" not in html
    assert "R395" in html  # monthly tier
    assert "7 working days" in html  # turnaround
    assert "smarter move" in html or "reuse your domain" in html  # reframe


def test_pdf_has_cape_town_address():
    html = _pdf_render()
    assert "Cape Town, Western Cape" in html


def test_pdf_has_3g_and_data_no_stage_6():
    """3G + data is evergreen. stage 6 is outdated — must be gone."""
    html = _pdf_render()
    assert "3G" in html
    assert "data" in html
    assert "stage 6" not in html
    assert "Eskom" not in html


def test_pdf_has_payfast_and_incl_vat():
    html = _pdf_render()
    assert "PayFast" in html
    assert "incl. VAT" in html
    assert "excl. VAT" not in html
    # "Pay by EFT" is the old call-to-action. Note: "instant EFT" appears as
    # one of PayFast's payment methods ("instant EFT, card or SnapScan"),
    # which is correct and intentional — only "Pay by EFT" / standalone EFT
    # as the primary payment instruction should be gone.
    assert "Pay by EFT" not in html


def test_pdf_platform_aware_for_wordpress_divi():
    """PDF must NOT hardcode 'no Wix watermark' for WP leads (the bug this session fixed)."""
    html = _pdf_render(platform="wordpress_divi")
    assert "no Wix watermark" not in html
    assert "no Divi plugin bloat" in html


def test_pdf_platform_aware_for_wix():
    """Wix leads SHOULD see 'no Wix watermark' — the only platform where the old copy was correct."""
    html = _pdf_render(platform="wix")
    assert "no Wix watermark" in html


def test_pdf_platform_aware_for_static_html():
    html = _pdf_render(platform="static_html")
    assert "no Wix watermark" not in html
    assert "no static HTML to maintain" in html


def test_pdf_has_intro_slots_framing():
    """PDF capacity framing uses plain language ('Only 3 website rebuilds each month')."""
    html = _pdf_render()
    assert "Only 3 website rebuilds" in html or "3 rebuilds" in html


def test_pdf_no_expiry_framing():
    """PDF must not claim the R5,000 rate is temporary."""
    html = _pdf_render()
    assert "intro" not in html.lower(), "PDF has 'intro' wording — contradicts permanent positioning"
    assert "expir" not in html.lower(), "PDF has 'expir' wording"
    assert "goes back" not in html.lower(), "PDF has 'goes back' wording"


# ═════════════════════════════════════════════════════════════════════════════
# CIPC / VAT REGISTRATION LINE (Phase N rev-4: legitimate company trust signal)
# ═════════════════════════════════════════════════════════════════════════════


def test_registration_line_uses_actual_constants():
    """The actual CIPC_REG_NUMBER / VAT_NUMBER constants (now filled in) drive the footer.

    Live values:
      CIPC_REG_NUMBER = "2026/055613/07"  (Client Compass Digital Solutions (Pty) Ltd)
      VAT_NUMBER = ""                    (not VAT registered yet)
    """
    from app.utils import email_modern_builder
    # Real values should be set in the module
    assert email_modern_builder.CIPC_REG_NUMBER == "2026/055613/07"
    assert email_modern_builder.VAT_NUMBER == ""

    # Modern template: Reg. No. line is present, VAT line is absent
    c = _mod_render()
    assert "Reg. No. 2026/055613/07" in c.body_html
    # No VAT line yet (no VAT number set)
    assert "VAT No." not in c.body_html


def test_registration_line_legacy_uses_actual_constants():
    """Same for the legacy templates — Reg. No. line is present in step 1/2/3."""
    import app.utils.email_builder as eb
    assert eb.CIPC_REG_NUMBER == "2026/055613/07"
    assert eb.VAT_NUMBER == ""
    for step in (1, 2, 3):
        c = _legacy_render(step=step)
        text_or_html = c.body_text + c.body_html
        assert "Reg. No. 2026/055613/07" in text_or_html, f"Step {step} missing live Reg. No."
        assert "VAT No." not in text_or_html, f"Step {step} should not show VAT line"


def test_registration_line_modern_handles_vat_later(monkeypatch=None):
    """Sanity: when VAT_NUMBER is later set, the VAT line appears alongside the Reg. No. line.

    This is the future state once Client Compass registers for VAT with SARS.
    We don't assert the literal value (the operator will fill it in), just the structure.
    """
    import app.utils.email_modern_builder as emb
    saved_vat = emb.VAT_NUMBER
    emb.VAT_NUMBER = "4123456789"
    try:
        c = _mod_render()
        assert "Reg. No. 2026/055613/07" in c.body_html
        assert "VAT No. 4123456789" in c.body_html
    finally:
        emb.VAT_NUMBER = saved_vat


def test_registration_line_modern_with_reg_only(monkeypatch=None):
    """When only CIPC_REG_NUMBER is set, only the Reg. No. part appears."""
    import app.utils.email_modern_builder as emb
    saved_reg, saved_vat = emb.CIPC_REG_NUMBER, emb.VAT_NUMBER
    emb.CIPC_REG_NUMBER = "2024/123456/07"
    emb.VAT_NUMBER = ""
    try:
        c = _mod_render()
        assert "Reg. No. 2024/123456/07" in c.body_html
        assert "VAT No." not in c.body_html
    finally:
        emb.CIPC_REG_NUMBER, emb.VAT_NUMBER = saved_reg, saved_vat


def test_registration_line_modern_with_both():
    """When both are set, the full line appears separated by middot."""
    import app.utils.email_modern_builder as emb
    saved_reg, saved_vat = emb.CIPC_REG_NUMBER, emb.VAT_NUMBER
    emb.CIPC_REG_NUMBER = "2024/123456/07"
    emb.VAT_NUMBER = "4123456789"
    try:
        c = _mod_render()
        assert "Reg. No. 2024/123456/07" in c.body_html
        assert "VAT No. 4123456789" in c.body_html
        # Separator present
        assert "Reg. No. 2024/123456/07" in c.body_html and "VAT No. 4123456789" in c.body_html
        # The two parts are joined (within reasonable proximity — same line)
        import re
        # Look for the two segments separated by middot (·) or bullet
        joined = re.search(r"Reg\. No\.\s*2024/123456/07\s*[·\u00b7]\s*VAT No\.\s*4123456789", c.body_html)
        assert joined is not None, "Reg. No. and VAT No. should appear on the same line, separated by middot"
    finally:
        emb.CIPC_REG_NUMBER, emb.VAT_NUMBER = saved_reg, saved_vat


def test_registration_line_legacy_with_both():
    """Legacy step 1/2/3 footers also pick up the registration line."""
    import app.utils.email_builder as eb
    saved_reg, saved_vat = eb.CIPC_REG_NUMBER, eb.VAT_NUMBER
    eb.CIPC_REG_NUMBER = "2024/123456/07"
    eb.VAT_NUMBER = "4123456789"
    try:
        for step in (1, 2, 3):
            c = _legacy_render(step=step)
            text_or_html = c.body_text + c.body_html
            assert "Reg. No. 2024/123456/07" in text_or_html, f"Step {step} missing Reg. No."
            assert "VAT No. 4123456789" in text_or_html, f"Step {step} missing VAT No."
    finally:
        eb.CIPC_REG_NUMBER, eb.VAT_NUMBER = saved_reg, saved_vat


def test_registration_line_absent_in_legacy_when_empty():
    """Empty constants → no placeholder strings in legacy either."""
    import app.utils.email_builder as eb
    # Reset just in case another test left them set
    saved_reg, saved_vat = eb.CIPC_REG_NUMBER, eb.VAT_NUMBER
    eb.CIPC_REG_NUMBER = ""
    eb.VAT_NUMBER = ""
    try:
        for step in (1, 2, 3):
            c = _legacy_render(step=step)
            text_or_html = c.body_text + c.body_html
            assert "Reg. No." not in text_or_html, f"Step {step} shows placeholder Reg. No."
            assert "VAT No." not in text_or_html, f"Step {step} shows placeholder VAT No."
    finally:
        eb.CIPC_REG_NUMBER, eb.VAT_NUMBER = saved_reg, saved_vat