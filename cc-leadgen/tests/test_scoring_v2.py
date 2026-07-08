"""Tests for the V2 web pitch scoring (migration 005 — 2026-07-08).

Covers the 9 improvements from the scoring audit:
  1. No-HTTPS bonus
  2. No-WhatsApp bonus (SA sites)
  3. No-JSON-LD bonus
  4. No-analytics bonus
  5. Generic-H1 bonus
  6. Rolling copyright threshold (year-based)
  7. V2 platform scoring (age + complexity split)
  8. Modern-stack dampener
  9. Template-leftover bonus

Run with: cd ~/installedApps/leadgen/cc-leadgen && source .venv/bin/activate && pytest tests/test_scoring_v2.py -v
"""
from __future__ import annotations

import os
import sys
from datetime import datetime

# Make the project importable when pytest is run from anywhere
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.workers.web_audit import (
    calculate_web_pitch_score,
    MODERN_STACK_DAMPENER,
    MODERN_STACK_SIGNAL_THRESHOLD,
)
from app.scrapers.web_audit import (
    PLATFORM_PITCH_SCORES_V2,
    is_generic_h1,
    detect_unedited_template,
    GENERIC_H1_PATTERNS,
    TEMPLATE_LEFTOVER_KEYWORDS,
    detect_whatsapp,
    detect_jsonld,
    detect_analytics,
    detect_manifest,
    detect_https,
    is_godaddy_builder_8,
)


# ─── 1. No-HTTPS bonus ───────────────────────────────────────────────────────

def test_no_https_adds_10_to_score():
    """An HTTP-only site gets +10 vs the same site on HTTPS."""
    base = dict(
        platform="wordpress_avada",
        copyright_year=None,
        pagespeed_mobile=47,
        pagespeed_desktop=None,
        pagespeed_seo=None,
        is_https=True,
        has_whatsapp=False,
        has_jsonld=False,
        has_analytics=False,
        has_manifest=False,
        h1_text=None,
        has_unedited_template=False,
    )
    https_score = calculate_web_pitch_score(**base).total

    http_breakdown = calculate_web_pitch_score(**{**base, "is_https": False})
    http_score = http_breakdown.total

    assert http_score - https_score == 10
    assert http_breakdown.no_https_bonus == 10


# ─── 2. No-WhatsApp bonus ────────────────────────────────────────────────────

def test_no_whatsapp_adds_10_to_score():
    """A site missing WhatsApp gets +10 vs the same site with WhatsApp."""
    base = dict(
        platform="wix",
        copyright_year=None,
        pagespeed_mobile=35,
        pagespeed_desktop=None,
        pagespeed_seo=None,
        is_https=True,
        has_whatsapp=True,
        has_jsonld=True,
        has_analytics=True,
        has_manifest=True,  # 4 modern signals — would normally trigger dampener
        h1_text=None,
        has_unedited_template=False,
    )
    wa_score = calculate_web_pitch_score(**base).total

    no_wa_breakdown = calculate_web_pitch_score(**{**base, "has_whatsapp": False})
    no_wa_score = no_wa_breakdown.total

    assert no_wa_score - wa_score == 10
    assert no_wa_breakdown.no_whatsapp_bonus == 10


# ─── 3. No-JSON-LD bonus ─────────────────────────────────────────────────────

def test_no_jsonld_adds_5_to_score():
    base = dict(
        platform="wordpress_divi",
        copyright_year=None,
        pagespeed_mobile=None,
        pagespeed_desktop=None,
        pagespeed_seo=None,
        is_https=True,
        has_whatsapp=True,
        has_jsonld=True,
        has_analytics=True,
        has_manifest=True,
        h1_text=None,
        has_unedited_template=False,
    )
    with_ld = calculate_web_pitch_score(**base).total
    no_ld = calculate_web_pitch_score(**{**base, "has_jsonld": False}).total
    assert no_ld - with_ld == 5


# ─── 4. No-analytics bonus ───────────────────────────────────────────────────

def test_no_analytics_adds_5_to_score():
    base = dict(
        platform="wix",
        copyright_year=None,
        pagespeed_mobile=None,
        pagespeed_desktop=None,
        pagespeed_seo=None,
        is_https=True,
        has_whatsapp=True,
        has_jsonld=True,
        has_analytics=True,
        has_manifest=True,
        h1_text=None,
        has_unedited_template=False,
    )
    with_ga = calculate_web_pitch_score(**base).total
    no_ga = calculate_web_pitch_score(**{**base, "has_analytics": False}).total
    assert no_ga - with_ga == 5


# ─── 5. Generic H1 bonus ─────────────────────────────────────────────────────

def test_generic_h1_triggers_bonus():
    base = dict(
        platform="wordpress_elementor",
        copyright_year=None,
        pagespeed_mobile=None,
        pagespeed_desktop=None,
        pagespeed_seo=None,
        is_https=True,
        has_whatsapp=True,
        has_jsonld=True,
        has_analytics=True,
        has_manifest=True,
        h1_text=None,
        has_unedited_template=False,
    )
    real_h1 = calculate_web_pitch_score(**{**base, "h1_text": "Expert Plumbing in Cape Town"}).total
    about_h1 = calculate_web_pitch_score(**{**base, "h1_text": "about us"}).total
    home_h1 = calculate_web_pitch_score(**{**base, "h1_text": "Home"}).total
    assert about_h1 - real_h1 == 5
    assert home_h1 - real_h1 == 5


def test_is_generic_h1_helper():
    assert is_generic_h1("about us")
    assert is_generic_h1("Home")
    assert is_generic_h1("WELCOME TO OUR SITE")
    assert is_generic_h1("lorem ipsum dolor sit amet")
    # Real H1s should not match
    assert not is_generic_h1("Expert Plumbing in Cape Town")
    assert not is_generic_h1("RENOVATIONS • CONSTRUCTION • PAINTING")  # 47 chars, too long? actually under 80
    assert not is_generic_h1(None)
    assert not is_generic_h1("")
    # Long text is body copy, not a real H1
    long_text = "Welcome to our website " * 10
    assert not is_generic_h1(long_text)


# ─── 6. Rolling copyright threshold ──────────────────────────────────────────

def test_copyright_threshold_rolls_forward():
    """The 3-year stale threshold uses datetime.now().year, not a hard-coded value."""
    current_year = datetime.now().year

    base = dict(
        platform="wordpress_avada",
        pagespeed_mobile=None,
        pagespeed_desktop=None,
        pagespeed_seo=None,
        is_https=True,
        has_whatsapp=True,
        has_jsonld=True,
        has_analytics=True,
        has_manifest=True,
        h1_text=None,
        has_unedited_template=False,
    )
    # 4 years stale → +10
    four_year_stale = calculate_web_pitch_score(**{**base, "copyright_year": current_year - 4}).total
    # 2 years old → no bonus (under threshold)
    two_year_old = calculate_web_pitch_score(**{**base, "copyright_year": current_year - 2}).total
    # 5+ years → +20
    very_stale = calculate_web_pitch_score(**{**base, "copyright_year": current_year - 6}).total

    # Difference should be +10 (4y) and +20 (6y) relative to fresh
    assert four_year_stale - two_year_old == 10
    assert very_stale - two_year_old == 20


# ─── 7. V2 platform scoring (age + complexity split) ─────────────────────────

def test_platform_v2_sums_match_v1():
    """V2 (age, complexity) sum equals V1 single score for every platform."""
    from app.scrapers.web_audit import PLATFORM_PITCH_SCORES
    for platform, (age, comp) in PLATFORM_PITCH_SCORES_V2.items():
        v1_total = PLATFORM_PITCH_SCORES[platform]
        assert age + comp == v1_total, f"{platform}: {age}+{comp} != {v1_total}"


def test_recent_platform_low_age_score():
    """wix / godaddy / squarespace have low age_score (≤10) — recent platforms."""
    assert PLATFORM_PITCH_SCORES_V2["wix"][0] <= 10
    assert PLATFORM_PITCH_SCORES_V2["godaddy"][0] <= 10
    assert PLATFORM_PITCH_SCORES_V2["squarespace"][0] <= 10


def test_legacy_platform_high_age_score():
    """afrihost / yola / joomla have high age_score (≥25) — ancient platforms."""
    assert PLATFORM_PITCH_SCORES_V2["afrihost_sitebuilder"][0] >= 25
    assert PLATFORM_PITCH_SCORES_V2["yola"][0] >= 25
    assert PLATFORM_PITCH_SCORES_V2["joomla"][0] >= 25


def test_shopify_and_drupal_are_skipped():
    """Both platforms should have (0, 0) — we skip them entirely."""
    assert PLATFORM_PITCH_SCORES_V2["shopify"] == (0, 0)
    assert PLATFORM_PITCH_SCORES_V2["drupal"] == (0, 0)


# ─── 8. Modern-stack dampener ────────────────────────────────────────────────

def test_modern_stack_dampener_triggers_at_threshold():
    """When 4+ modern signals are present, age_score is reduced by MODERN_STACK_DAMPENER."""
    base_modern = dict(
        platform="wordpress_avada",  # age=15, complexity=5
        copyright_year=None,
        pagespeed_mobile=None,
        pagespeed_desktop=None,
        pagespeed_seo=None,
        is_https=True,
        has_whatsapp=True,
        has_jsonld=True,
        has_analytics=True,
        has_manifest=True,  # 5 modern signals
        h1_text=None,
        has_unedited_template=False,
    )
    breakdown = calculate_web_pitch_score(**base_modern)
    assert breakdown.modern_stack_signals_count == MODERN_STACK_SIGNAL_THRESHOLD + 1
    assert breakdown.modern_stack_dampener == -MODERN_STACK_DAMPENER


def test_modern_stack_dampener_not_triggered_with_fewer_signals():
    """With 3 or fewer modern signals, dampener does NOT fire."""
    base = dict(
        platform="wordpress_avada",
        copyright_year=None,
        pagespeed_mobile=None,
        pagespeed_desktop=None,
        pagespeed_seo=None,
        is_https=True,
        has_whatsapp=True,
        has_jsonld=True,
        has_analytics=False,
        has_manifest=False,  # only 3 modern signals
        h1_text=None,
        has_unedited_template=False,
    )
    breakdown = calculate_web_pitch_score(**base)
    assert breakdown.modern_stack_signals_count == 3
    assert breakdown.modern_stack_dampener == 0


def test_modern_stack_dampener_caps_at_age_score():
    """If age_score is smaller than MODERN_STACK_DAMPENER, dampener shouldn't go below zero."""
    base = dict(
        platform="wix",  # age=10, complexity=15
        copyright_year=None,
        pagespeed_mobile=None,
        pagespeed_desktop=None,
        pagespeed_seo=None,
        is_https=True,
        has_whatsapp=True,
        has_jsonld=True,
        has_analytics=True,
        has_manifest=True,
        h1_text=None,
        has_unedited_template=False,
    )
    breakdown = calculate_web_pitch_score(**base)
    # age_score=10, dampener=-15 → clamped to -10 (cannot go below age_score)
    assert breakdown.modern_stack_dampener == -10
    assert breakdown.platform_age_score == 10  # original age_score (returned for display)


# ─── 9. Template-leftover bonus ──────────────────────────────────────────────

def test_template_leftover_high_confidence_substring():
    """Lorem ipsum / edit me / filler@ in HTML → True anywhere."""
    html = "<html><body>Lorem ipsum dolor sit amet</body></html>"
    assert detect_unedited_template(html, nav_text="", h1=None, h2s=[])

    html2 = "<html><body>Click to edit this text</body></html>"
    assert detect_unedited_template(html2, nav_text="", h1=None, h2s=[])

    html3 = "<html><body>Contact: info@filler@example.com</body></html>"
    assert detect_unedited_template(html3, nav_text="", h1=None, h2s=[])


def test_template_leftover_nav_only():
    """'Our dresses' / 'Join our awesome team' in nav only → True."""
    nav_with_leftover = "Home About Our Dresses Gallery Contact"
    assert detect_unedited_template(html="<html><body>Real content</body></html>",
                                    nav_text=nav_with_leftover, h1=None, h2s=[])

    nav_clean = "Home About Services Gallery Contact"
    assert not detect_unedited_template(html="<html><body>Real content</body></html>",
                                        nav_text=nav_clean, h1="Real H1", h2s=["Real H2"])


def test_template_leftover_no_false_positive_in_footer():
    """A footer mentioning 'create account' should not trigger."""
    html = '<html><body><nav>Home Services</nav><footer>Create account | Sign in</footer></body></html>'
    # nav_text here is nav-only — footer not included
    nav_only = "Home Services"
    # No high-confidence substrings, no nav-only matches, H1 is fine
    assert not detect_unedited_template(html=html, nav_text=nav_only,
                                        h1="Real H1", h2s=[])


def test_template_leftover_adds_10_to_score():
    base = dict(
        platform="godaddy",
        copyright_year=None,
        pagespeed_mobile=None,
        pagespeed_desktop=None,
        pagespeed_seo=None,
        is_https=True,
        has_whatsapp=True,
        has_jsonld=True,
        has_analytics=True,
        has_manifest=True,
        h1_text=None,
        has_unedited_template=False,
    )
    clean = calculate_web_pitch_score(**base).total
    template_leftover = calculate_web_pitch_score(**{**base, "has_unedited_template": True}).total
    assert template_leftover - clean == 10


# ─── Integration: the three real-world leads from the audit ─────────────────

def test_ground_up_construction_profiles_correctly():
    """From the 2026-07-08 audit — http site, no WhatsApp, no JSON-LD, no analytics.
    Should score HIGH (real painful-awareness candidate)."""
    breakdown = calculate_web_pitch_score(
        platform="wordpress_avada",
        copyright_year=2018,  # 8 years stale in 2026
        pagespeed_mobile=47,
        pagespeed_desktop=None,
        pagespeed_seo=None,  # assume low
        is_https=False,  # HTTP-only
        has_whatsapp=False,
        has_jsonld=False,
        has_analytics=False,
        has_manifest=False,
        h1_text="RENOVATIONS • CONSTRUCTION • PAINTING",
        has_unedited_template=False,
    )
    # Expected: 15 (age) + 5 (comp) + 20 (8y stale) + 10 (mobile 50-69) + 10 (no HTTPS) + 10 (no WA) + 5 (no JSON-LD) + 5 (no analytics) = 80
    # (rough calc — actual may differ slightly based on desktop/SEO)
    assert breakdown.no_https_bonus == 10
    assert breakdown.no_whatsapp_bonus == 10
    assert breakdown.no_jsonld_bonus == 5
    assert breakdown.no_analytics_bonus == 5
    assert breakdown.copyright_bonus == 20  # 8 years stale
    # All signals present would be a dampener, but Ground Up has NO modern signals
    assert breakdown.modern_stack_signals_count == 0
    assert breakdown.modern_stack_dampener == 0
    # Score should be solidly above 70 (mockup threshold)
    assert breakdown.total >= 70


def test_maboneng_photoshoot_dampener_applies():
    """From the 2026-07-08 audit — GoDaddy Builder 8.0 with HTTPS, WhatsApp, manifest, analytics.
    Should score LOWER than ground_up due to modern-stack dampener."""
    breakdown = calculate_web_pitch_score(
        platform="godaddy",  # recent — age=5, complexity=15
        copyright_year=None,
        pagespeed_mobile=45,
        pagespeed_desktop=None,
        pagespeed_seo=None,
        is_https=True,
        has_whatsapp=True,
        has_jsonld=False,
        has_analytics=True,
        has_manifest=True,
        h1_text="hey bestie...",
        has_unedited_template=True,  # 'OUR DRESSES' nav etc
    )
    # 4 modern signals (HTTPS, WA, analytics, manifest) → dampener triggers
    assert breakdown.modern_stack_signals_count == 4
    assert breakdown.modern_stack_dampener == -5  # caps at age_score (5)
    # Score should be LOWER than the equivalent site without modern signals
    equivalent_no_modern = calculate_web_pitch_score(
        platform="godaddy",
        copyright_year=None,
        pagespeed_mobile=45,
        pagespeed_desktop=None,
        pagespeed_seo=None,
        is_https=False, has_whatsapp=False, has_jsonld=False,
        has_analytics=False, has_manifest=False,
        h1_text=None, has_unedited_template=False,
    )
    assert breakdown.total < equivalent_no_modern.total


def test_belle_doux_dampener_applies():
    """From the 2026-07-08 audit — modern Wix with HTTPS, JSON-LD, analytics.
    Should have dampener applied + no-WhatsApp penalty."""
    breakdown = calculate_web_pitch_score(
        platform="wix",  # age=10, complexity=15
        copyright_year=2021,  # 5 years stale in 2026
        pagespeed_mobile=35,
        pagespeed_desktop=None,
        pagespeed_seo=None,
        is_https=True,
        has_whatsapp=False,
        has_jsonld=True,
        has_analytics=True,
        has_manifest=False,
        h1_text="about us",  # generic — triggers bonus
        has_unedited_template=False,
    )
    # 3 modern signals (HTTPS, JSON-LD, analytics) — under threshold, NO dampener
    assert breakdown.modern_stack_signals_count == 3
    assert breakdown.modern_stack_dampener == 0
    # Generic H1 triggers bonus
    assert breakdown.generic_h1_bonus == 5
    # No WhatsApp triggers bonus
    assert breakdown.no_whatsapp_bonus == 10
    # 5-year copyright → +20
    assert breakdown.copyright_bonus == 20


# ─── Detection helper unit tests ─────────────────────────────────────────────

def test_detect_https_helper():
    assert detect_https({}, "https://example.com") is True
    assert detect_https({}, "http://example.com") is False
    assert detect_https({"X-Forwarded-Proto": "https"}, "http://example.com") is True


def test_detect_whatsapp_helper():
    # URL-based click-to-chat links → True
    assert detect_whatsapp('<a href="https://wa.me/27821234567">Chat</a>')
    assert detect_whatsapp('<a href="https://web.whatsapp.com/send?phone=27821234567">Chat</a>')
    assert detect_whatsapp('<a href="whatsapp://send?text=hi">Chat</a>')
    # Loose text mention without a link → False (over-counting fix 2026-07-08)
    assert not detect_whatsapp('<div>Send us a WhatsApp</div>')
    assert not detect_whatsapp('<a href="mailto:info@example.com">Email</a>')


def test_detect_jsonld_helper():
    assert detect_jsonld('<script type="application/ld+json">{"@context": "..."}</script>')
    assert not detect_jsonld('<script>var x = 1;</script>')


def test_detect_analytics_helper():
    assert detect_analytics('<script src="https://www.google-analytics.com/analytics.js"></script>')
    assert detect_analytics('<script src="https://www.googletagmanager.com/gtag/js?id=GA123"></script>')
    assert detect_analytics('<script src="https://connect.facebook.net/en_US/fbevents.js"></script>')
    assert detect_analytics('<script src="https://www.googletagmanager.com/gtm.js"></script>')
    assert not detect_analytics('<script>console.log("no analytics")</script>')


def test_detect_manifest_helper():
    assert detect_manifest('<link rel="manifest" href="/site.webmanifest">')
    assert detect_manifest('<link rel=\'manifest\' href=\'/site.webmanifest\'>')
    assert not detect_manifest('<link rel="stylesheet" href="/style.css">')


def test_is_godaddy_builder_8_helper():
    """Builder 8 sites have Starfield meta generator + wsimg CDN."""
    html_with_builder_8 = '<meta name="generator" content="Starfield Technologies; Go Daddy Website Builder 8.0.0000">'
    html_legacy = '<html><body>Old GoDaddy site</body></html>'
    assert is_godaddy_builder_8(html_with_builder_8, {})
    assert not is_godaddy_builder_8(html_legacy, {})


# ─── Score is capped at 100 ──────────────────────────────────────────────────

def test_score_capped_at_100():
    """A super-bad site should still cap at 100, not run away."""
    breakdown = calculate_web_pitch_score(
        platform="afrihost_sitebuilder",  # (30, 25)
        copyright_year=2010,  # 16 years stale → +20
        pagespeed_mobile=20,  # <50 → +25
        pagespeed_desktop=20,  # <50 → +15
        pagespeed_seo=30,  # <70 → +15
        is_https=False, has_whatsapp=False, has_jsonld=False,
        has_analytics=False, has_manifest=False,
        h1_text="home", has_unedited_template=True,  # +5 +10
    )
    # Sum: 30 + 25 + 20 + 25 + 15 + 15 + 10 + 10 + 5 + 5 + 10 = 170 → cap at 100
    assert breakdown.total == 100
    assert breakdown.capped_at_100 is True


# ─── Backward compat: legacy callers still get a number ──────────────────────

def test_legacy_call_still_returns_int_via_total():
    """The new function returns a dataclass, but .total is the int callers expect."""
    from app.workers.web_audit import calculate_web_pitch_score
    result = calculate_web_pitch_score(
        platform="wix",
        copyright_year=2020,
        pagespeed_mobile=30,
        pagespeed_desktop=None,
        pagespeed_seo=None,
    )
    assert isinstance(result.total, int)
    assert 0 <= result.total <= 100

# ─── Tightened WhatsApp detection (post-deploy fix) ───────────────────────

def test_detect_whatsapp_requires_href():
    """A loose "WhatsApp" text mention (no link) must NOT count as has_whatsapp."""
    # Text-only mention, no link → should be False
    html_text_only = "<html><body><p>Get in touch on WhatsApp</p></body></html>"
    assert not detect_whatsapp(html_text_only), "loose text mention should not count"

    # Footer mention without href → should be False
    html_footer_text = '<html><body><footer>We use WhatsApp for support</footer></body></html>'
    assert not detect_whatsapp(html_footer_text), "footer text mention should not count"

    # Real wa.me link → should be True
    html_wa_me = '<a href="https://wa.me/27821234567">Chat</a>'
    assert detect_whatsapp(html_wa_me), "wa.me link should count"

    # Real web.whatsapp.com link → should be True
    html_web_wa = '<a href="https://web.whatsapp.com/send?phone=27821234567">Chat</a>'
    assert detect_whatsapp(html_web_wa), "web.whatsapp.com link should count"

    # whatsapp:// scheme → should be True
    html_scheme = '<a href="whatsapp://send?text=hi">Chat</a>'
    assert detect_whatsapp(html_scheme), "whatsapp:// scheme should count"

    # No WhatsApp at all → should be False
    html_none = '<html><body><a href="mailto:info@example.com">Email</a></body></html>'
    assert not detect_whatsapp(html_none), "no WhatsApp reference should be False"
