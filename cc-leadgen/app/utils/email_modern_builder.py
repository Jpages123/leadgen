"""Modern HTML email renderer — Stripe/Linear-style email for web revamp drafts.

This produces a polished, branded Client Compass email used when a mockup
has been approved and a draft is being generated for operator review.

Design choices:
  - Light background (better Gmail default + Outlook rendering)
  - Slate text + emerald accents (matches clientcompass.co.za brand)
  - Table-based layout for email-client compatibility
  - Inline CSS only (no <style> tags — they get stripped by Gmail)
  - Logo hosted at clientcompass.co.za (already public)
  - Mockup screenshot inlined as base64 (works without external image hosting)
  - Plain-text fallback body for text-only email clients

Recipients are mostly SA trades owners reading on phones, so the design is
mobile-first, thumb-friendly, and renders cleanly in:
  - Gmail (web + mobile app)
  - Apple Mail
  - Outlook 2016+ (with mso-table-lspace hack)
"""
from __future__ import annotations

import base64
import html
from dataclasses import dataclass
from datetime import datetime
from typing import Optional

from .email_builder import (
    SENDER_NAME,
    PLATFORM_DISPLAY_MAP,
    EmailContent,
    build_unsubscribe_url,
)
from .email_metrics import pick_stat_rows, pick_stat_rows_text


# ── Brand constants ──────────────────────────────────────────────────────────
BRAND_PRIMARY = "#22c55e"     # emerald — CTA buttons, accents
BRAND_PRIMARY_DARK = "#15803d"  # darker green — hover/contrast
BRAND_TEXT = "#0f172a"        # slate-900 — main text
BRAND_TEXT_MUTED = "#475569"  # slate-600 — secondary text
BRAND_TEXT_LIGHT = "#94a3b8"  # slate-400 — tertiary, captions
BRAND_BG = "#f1f5f9"          # slate-100 — outer wrapper
BRAND_BORDER = "#e2e8f0"      # slate-200 — dividers

LOGO_URL = "https://clientcompass.co.za/assets/logo-horizontal.png"
LOGO_URL_1X = "https://clientcompass.co.za/assets/logo-horizontal-1x.png"  # 640px, low-bandwidth fallback for srcset=1x clients
FAVICON_URL = "https://clientcompass.co.za/favicon-32x32.png"
SITE_URL = "https://clientcompass.co.za"

# ── Logo variants for dark-mode email clients (Gmail web/iOS/Android,
# Apple Mail, modern Outlook). The default logo (medium teal) is invisible on
# white backgrounds — we ship a darker teal for light mode and a lighter teal
# for dark mode, swapped via <picture>'s prefers-color-scheme media attribute.
# Files hosted at clientcompass.co.za via the email-assets upload endpoint.
LOGO_LIGHT_1X = "https://clientcompass.co.za/assets/mockups/logo-horizontal-1x-light.png"  # dark teal #0E7490 (light bg)
LOGO_LIGHT_2X = "https://clientcompass.co.za/assets/mockups/logo-current-light.png"         # dark teal #0E7490 (light bg, 2x)
LOGO_DARK_1X  = "https://clientcompass.co.za/assets/mockups/logo-horizontal-1x-dark.png"   # light teal #BAE6FD (dark bg)
LOGO_DARK_2X  = "https://clientcompass.co.za/assets/mockups/logo-current-dark.png"          # light teal #BAE6FD (dark bg, 2x)
WHATSAPP_NUMBER = "27740940550"  # SA format, no '+'
PHONE_DISPLAY = "+27 74 094 0550"

COMPANY_NAME = "Client Compass Digital Solutions Pty (Ltd)"
COMPANY_EMAIL = "info@clientcompass.co.za"

# ── Revamp pricing / offer constants ──────────────────────────────────────────
# Edit these in ONE place to update all operator-reviewed drafts. The legacy
# email_builder.py has its own copies in the Jinja templates — update those too
# when the prices change.
#
# Pricing model: the revamp service is naturally cheaper than a new build
# (we reuse the client's existing domain and content), so R5,000 is the
# permanent revamp rate — NOT a temporary "intro" discount. R7,000 is the
# standard new-build rate (Starter tier on clientcompass.co.za). The scarcity
# is real (3 slots/month capacity cap) but the price has no expiry.
REVAMP_REVAMP_PRICE = "R5,000"            # permanent revamp-service rate
REVAMP_NEWBUILD_PRICE = "R8,000"         # standard new-build rate (Starter tier on marketing site — bumped 2026-08-04)
REVAMP_TURNAROUND_DAYS = 7               # working days from kickoff to delivery
REVAMP_MONTHLY_PRICE = "R395"            # hosting + maintenance + monthly performance report
REVAMP_SLOTS_PER_MONTH = 3               # capacity cap per calendar month (permanent)
REVAMP_VAT_NOTE = "(incl. VAT)"          # SA small biz don't think in VAT — keep it simple
REVAMP_PAYMENT_PROVIDER = "PayFast"      # SA payment gateway — instant EFT / card / SnapScan

# ── Location / compliance ────────────────────────────────────────────────────
COMPANY_CITY = "Cape Town, Western Cape"   # ECTA s.45 — physical address min

# ── CIPC / SARS registration (legitimacy signal, optional) ──────────────────
# Leave empty until you fill in your actual numbers — the footer line is
# rendered conditionally, so empty values don't show placeholder strings.
# Public lookup: https://www.cipc.co.za/ (company reg) and
# https://www.sars.gov.za/ (VAT verification). Anyone can verify, so this is
# a strong trust signal — far better than the fabricated social-proof line
# we deleted (Phase N rev-2).
#
# Common formats:
#   CIPC_REG_NUMBER: "2024/123456/07"  (year / sequence / 07 = private company)
#   VAT_NUMBER:      "4XXXXXXXXX"      (10 digits starting with 4)
CIPC_REG_NUMBER = "2026/055613/07"   # CIPC registration — Client Compass Digital Solutions (Pty) Ltd
VAT_NUMBER = ""                       # not VAT registered yet — leave empty


def _company_registration_line() -> str:
    """Build the CIPC/VAT registration line for the footer.

    Returns an empty string if neither constant is set, so the footer doesn't
    show placeholder text. The full line reads::

        Reg. No. 2024/123456/07 · VAT No. 4123456789

    Only the parts with values are included (e.g. if only VAT_NUMBER is set,
    the line reads "VAT No. 4123456789").
    """
    parts = []
    if CIPC_REG_NUMBER:
        parts.append(f"Reg. No. {_esc(CIPC_REG_NUMBER)}")
    if VAT_NUMBER:
        parts.append(f"VAT No. {_esc(VAT_NUMBER)}")
    return " · ".join(parts)

# ── Default unsubscribe base URL ─────────────────────────────────────────────
# This MUST be a publicly reachable host. cc-leadgen.clientcompass.co.za has no
# DNS record (the laptop is reachable only via Tailscale), so we point at the
# always-on VPS at login.clientcompass.co.za which proxies back to the laptop
# over Tailscale. The JWT token is built by build_unsubscribe_url() below.
UNSUBSCRIBE_BASE_URL = "https://login.clientcompass.co.za"


# ── Helpers ──────────────────────────────────────────────────────────────────

def _esc(text: Optional[str]) -> str:
    """HTML-escape a string for safe interpolation."""
    if text is None:
        return ""
    return html.escape(str(text), quote=True)


def _mobile_score_color(score) -> str:
    """Return brand color for a mobile PageSpeed score (0-100)."""
    try:
        score = int(score)
    except (ValueError, TypeError):
        return BRAND_TEXT_MUTED
    if score < 50:
        return "#dc2626"  # red-600
    if score < 70:
        return "#f59e0b"  # amber-500
    return BRAND_PRIMARY


def _load_time_estimate(mobile_score) -> Optional[int]:
    """Translate a mobile PageSpeed score into an approximate load time in seconds.

    The mapping is rough (PageSpeed doesn't directly report load time and we
    don't have access to the raw audit), but it lands in the same range
    prospects would see on their own phones: a score of 50 ≈ 6s, 30 ≈ 10s,
    70 ≈ 3.5s, 90+ ≈ 2s.

    Returns None when the score is missing/invalid.
    """
    try:
        score = int(mobile_score)
    except (ValueError, TypeError):
        return None
    # Clamp to [0, 100]
    score = max(0, min(100, score))
    # Linear-ish: seconds ≈ 15 - (score / 10), clamped to [2, 15]
    seconds = max(2.0, min(15.0, 15.0 - (score / 10.0)))
    return int(round(seconds))


def _platform_aware_pitch(platform: str) -> str:
    """Return a platform-aware line for the mockup intro.

    Replaces the old hardcoded "no Wix watermark" copy that was wrong for
    ~80% of leads (WP dominates .za). Returns a short phrase describing
    what the rebuilt site avoids / fixes, calibrated to the platform.

    Examples:
        wix                  → "no Wix watermark"
        wordpress_divi       → "no Divi plugin bloat"
        wordpress_elementor  → "no Elementor lock-in"
        wordpress_generic    → "no WordPress plugin headaches"
        joomla               → "no Joomla maintenance burden"
        static_html          → "no static HTML to maintain"
        anything else        → "no monthly platform fees"
    """
    table = {
        "wix":                  "no Wix watermark",
        "wix_subsidised":       "no Wix monthly subscription",
        "squarespace":          "no Squarespace subscription lock-in",
        "weebly":               "no Weebly builder limits",
        "godaddy":              "no GoDaddy site-builder fees",
        "wordpress_divi":       "no Divi plugin bloat or licence fees",
        "wordpress_elementor":  "no Elementor Pro lock-in",
        "wordpress_avada":      "no heavy theme licence fees",
        "wordpress_betheme":    "no premium theme subscription",
        "wordpress_generic":    "no WordPress plugin headaches",
        "joomla":               "no Joomla maintenance burden",
        "yola":                 "no Yola builder limits",
        "google_sites":         "no Google Sites URL ugliness",
        "afrihost_sitebuilder": "no Afrihost builder fees",
        "static_html":          "no static HTML to maintain",
    }
    return table.get(platform, "no monthly platform fees")


def _intro_offer_copy() -> dict:
    """Build the offer-block copy.

    Returns a dict with subject-line-friendly headline, the slot framing,
    the closing line. Pulled into a helper so the legacy builder can mirror
    the same wording in step 2/3 follow-ups.

    Key positioning (Phase N rev-6):
      - R5k is the rebuild rate (intrinsic pricing, NOT a discount)
      - R8k is the new-build rate for context (anchor — bumped from R7k 2026-08-04)
      - 7-working-day turnaround (operator-confirmed)
      - 3-slots-per-month capacity = the only real urgency lever
    """
    return {
        "headline": (
            f"{REVAMP_REVAMP_PRICE} rebuild &mdash; "
            f"{REVAMP_SLOTS_PER_MONTH} per month, {REVAMP_TURNAROUND_DAYS}-day turnaround"
        ),
        "body": (
            f"I only do <strong>{REVAMP_SLOTS_PER_MONTH} website rebuilds each month</strong>, "
            f"so each one gets my full attention &mdash; and most are done within "
            f"<strong>{REVAMP_TURNAROUND_DAYS} working days</strong>. The rebuild price is "
            f"<strong>{REVAMP_REVAMP_PRICE}</strong>, plus <strong>{REVAMP_MONTHLY_PRICE}/month</strong> "
            f"for hosting and a monthly check-up on your site. A brand new website is "
            f"{REVAMP_NEWBUILD_PRICE}, but if you&rsquo;ve already got a site, a rebuild is the "
            f"smarter move: I reuse your domain, content, photos and branding, so you only pay "
            f"for the part that actually needs changing. All prices {REVAMP_VAT_NOTE}."
        ),
        "closing": (
            f"If you want one of this month&rsquo;s {REVAMP_SLOTS_PER_MONTH} spots, just reply to this email &mdash; "
            f"I'll send a short proposal and a {REVAMP_PAYMENT_PROVIDER} payment link "
            f"(instant EFT, card or SnapScan)."
        ),
    }


# ── Main renderer ────────────────────────────────────────────────────────────

def render_modern_web_revamp(
    lead,
    screenshot_ref: str | None = None,
    base_url: str = UNSUBSCRIBE_BASE_URL,
) -> EmailContent:
    """Render the modern, branded email for a mockup-approved web revamp lead.

    Args:
        lead: Lead model instance (or shim) with attributes:
            business_name, owner_name, email, website_platform,
            pagespeed_mobile, web_pitch_score, mockup_url, id
        screenshot_ref: Either a `data:image/...;base64,...` URI (inline) or
            an absolute `https://...` URL (publicly hosted). When provided,
            the screenshot is embedded in the dark preview block.
        base_url: Public host for the unsubscribe link. Defaults to
            ``UNSUBSCRIBE_BASE_URL`` (login.clientcompass.co.za) — the only
            always-on host. cc-leadgen.clientcompass.co.za has no DNS.

    Returns:
        EmailContent with subject, body_text (plain fallback), body_html (modern),
        template_key='web_revamp_modern'
    """
    business_name = _esc(getattr(lead, "business_name", "your business"))
    owner_name = _esc(getattr(lead, "owner_name", "") or "")
    greeting_name = owner_name if owner_name else "there"

    platform_raw = getattr(lead, "website_platform", "") or ""
    platform_display = PLATFORM_DISPLAY_MAP.get(
        platform_raw, platform_raw or "your current platform"
    )
    platform_pitch = _platform_aware_pitch(platform_raw)

    mobile_score = getattr(lead, "pagespeed_mobile", None)
    pitch_score = getattr(lead, "web_pitch_score", 0) or 0
    mockup_url = getattr(lead, "mockup_url", None) or "#"
    load_seconds = _load_time_estimate(mobile_score)

    # Unsubscribe: signed JWT pointing at the public VPS host. Build via the
    # legacy builder's helper so the same token format is used everywhere
    # (the endpoint verifies the JWT signature, not the raw UUID).
    unsubscribe_url = build_unsubscribe_url(lead, base_url)

    # ── Stat card rows (smart-picked; replaces the fixed 3-row card from
    # the 2026-07-14 build that included "Opportunity score", an internal
    # leadgen ranking metric that didn't communicate anything actionable
    # to the prospect). See app/utils/email_metrics.py for the picker. ──
    stat_rows = pick_stat_rows(lead)

    # ── Pain narrative — concrete seconds instead of abstract percentile ──
    if load_seconds is not None:
        pain_sentence = (
            f"On a typical 4G phone, your site takes about "
            f"<strong style=\"color:{BRAND_TEXT};\">{load_seconds} seconds</strong> "
            f"before your phone number is visible. Most visitors give up at 3 — "
            f"you're losing quote requests every week."
        )
    else:
        pain_sentence = (
            "Mobile performance below 50 typically means customers on phones "
            "leave before your page finishes loading — which directly costs "
            "you quote requests."
        )

    # ── Plain-text fallback (for text-only email clients) ────────────────
    audit_bullets = "\n".join(f"  {line}" for line in pick_stat_rows_text(lead))
    intro_offer = _intro_offer_copy()
    offer_block_text = (
        f"{REVAMP_REVAMP_PRICE} rebuild, plus {REVAMP_MONTHLY_PRICE}/month for hosting and a "
        f"monthly check-up (a brand new website is {REVAMP_NEWBUILD_PRICE}, but if you've "
        f"already got a site, a rebuild is the smarter move). "
        f"Most rebuilds are done within {REVAMP_TURNAROUND_DAYS} working days. "
        f"All prices {REVAMP_VAT_NOTE}. "
        f"Pay via {REVAMP_PAYMENT_PROVIDER} (instant EFT, card or SnapScan) when you accept."
    )
    body_text = (
        f"Hi {greeting_name},\n\n"
        f"I research SA trades businesses and ran a quick check on "
        f"{business_name}'s website. Here's what the numbers showed:\n"
        f"{audit_bullets}\n\n"
        f"{pain_sentence.replace('<strong style=\"color:' + BRAND_TEXT + ';\">', '').replace('</strong>', '')}\n\n"
        f"I built a quick preview of what your new website could look like — "
        f"fast on phones, {platform_pitch}:\n"
        f"{mockup_url}\n\n"
        f"{offer_block_text}\n\n"
        f"{intro_offer['closing']}\n\n"
        f"I've also attached the full website report as a PDF (1 page) so you "
        f"can share it with a partner or refer back to it later — it includes "
        f"screenshots showing exactly what's wrong with the current site.\n\n"
        f"No pressure — if you're happy with your current site, just ignore this.\n\n"
        f"— {SENDER_NAME}\n"
        f"Client Compass · Building modern websites for SA small businesses from "
        f"{COMPANY_CITY}\n"
        f"WhatsApp: {PHONE_DISPLAY}\n"
    )

    # ── Subject ──────────────────────────────────────────────────────────
    # Title-case the name so ALL-CAPS business names read naturally in the subject
    raw_name = getattr(lead, "business_name", "") or ""
    name_for_subject = raw_name.title() if raw_name.isupper() else raw_name
    subject = f"{name_for_subject} — we built a free preview of your new website"

    # ── Mobile score color ───────────────────────────────────────────────
    mobile_color = _mobile_score_color(mobile_score)
    mobile_display = f"{mobile_score} / 100" if mobile_score else "—"

    # ── Pre-render stat rows as HTML <tr>...</tr> blocks for the f-string
    # template below. Keeps the template readable while letting email_metrics
    # own the selection logic.
    def _row_html(row: dict) -> str:
        weight = "font-weight:700;" if row["weight"] == "bold" else "font-weight:600;"
        return (
            f"              <tr>\n"
            f"                <td style=\"padding:6px 0;font-size:14px;color:{BRAND_TEXT_MUTED};\">{_esc(row['label'])}</td>\n"
            f"                <td align=\"right\" style=\"padding:6px 0;font-size:14px;{weight}color:{row['color']};\">{_esc(row['value'])}</td>\n"
            f"              </tr>"
        )

    stat_rows_html = "\n".join(_row_html(r) for r in stat_rows)

    # Preheader copy — surface the mobile score (worst pain signal) since
    # it's the most inbox-preview-relevant. Falls back to the worst row.
    preheader_parts = []
    for r in stat_rows:
        if "Mobile" in r["label"]:
            preheader_parts.append(r["value"])
            break
    if not preheader_parts and stat_rows:
        preheader_parts.append(stat_rows[0]["value"])
    preheader_value = preheader_parts[0] if preheader_parts else "your audit"

    # Stat card accent colour — mirrors the worst score in the card
    _row_colors = [r["color"] for r in stat_rows]
    if "#dc2626" in _row_colors:
        stat_card_border = "#dc2626"
    elif "#d97706" in _row_colors:
        stat_card_border = "#d97706"
    else:
        stat_card_border = BRAND_PRIMARY

    # ── Pre-render the optional CIPC/VAT registration line ─────────────
    # Shown only when at least one of CIPC_REG_NUMBER / VAT_NUMBER is set.
    # Empty constants produce an empty string and a blank line in the footer
    # is harmless — but we wrap in a conditional paragraph so empty defaults
    # don't leave an empty <p></p> in the rendered HTML.
    _reg_line = _company_registration_line()
    if _reg_line:
        registration_paragraph = (
            f'<p style="margin:0 0 4px 0;font-size:11px;color:{BRAND_TEXT_MUTED};">'
            f'{_reg_line}</p>'
        )
    else:
        registration_paragraph = ""

    # ── Mockup screenshot block ──────────────────────────────────────────
    if screenshot_ref:
        screenshot_html = f'''
          <img src="{screenshot_ref}" alt="Preview of your new website" width="536" style="display:block;width:100%;max-width:536px;height:auto;border-radius:6px;border:0;" />
'''
    else:
        # Fallback: dark CTA button linking to live mockup
        screenshot_html = '''
          <table role="presentation" width="100%" cellspacing="0" cellpadding="0" border="0">
            <tr>
              <td align="center" style="padding:30px 20px;">
                <a href="__MOCKUP_URL__" target="_blank" style="display:inline-block;padding:14px 28px;background:#22c55e;color:#ffffff;font-size:14px;font-weight:600;text-decoration:none;border-radius:6px;">
                  ▷ View your free preview →
                </a>
              </td>
            </tr>
          </table>
'''

    # ── Offer card copy ──────────────────────────────────────────────────
    offer = _intro_offer_copy()

    body_html = f'''<!DOCTYPE html>
<html lang="en" xmlns="http://www.w3.org/1999/xhtml" xmlns:o="urn:schemas-microsoft-com:office:office">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <meta name="x-apple-disable-message-reformatting">
  <meta name="format-detection" content="telephone=no">
  <title>Your website audit</title>
  <!--[if mso]>
  <style type="text/css">
    table, td, div, h1, p {{ font-family: Arial, sans-serif; }}
  </style>
  <xml>
    <o:OfficeDocumentSettings>
      <o:PixelsPerInch>96</o:PixelsPerInch>
    </o:OfficeDocumentSettings>
  </xml>
  <![endif]-->
</head>
<body style="margin:0;padding:0;background:{BRAND_BG};-webkit-font-smoothing:antialiased;-moz-osx-font-smoothing:grayscale;">

<!-- Preheader text (hidden, shows in inbox preview) -->
<div style="display:none;max-height:0;overflow:hidden;mso-hide:all;font-size:1px;line-height:1px;color:{BRAND_BG};">
  Your site scores {preheader_value} on mobile speed · {REVAMP_REVAMP_PRICE} website rebuild ({REVAMP_TURNAROUND_DAYS}-day turnaround) &mdash; only {REVAMP_SLOTS_PER_MONTH} spots each month.
</div>

<!-- EMAIL CONTAINER -->
<table role="presentation" width="100%" cellspacing="0" cellpadding="0" border="0" style="background:{BRAND_BG};">
<tr>
<td align="center" style="padding:24px 12px;">

<!--[if mso | IE]>
<table role="presentation" width="600" cellspacing="0" cellpadding="0" border="0"><tr><td>
<![endif]-->

<table role="presentation" width="600" cellspacing="0" cellpadding="0" border="0" style="max-width:600px;background:#ffffff;border-radius:12px;overflow:hidden;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,'Helvetica Neue',Arial,sans-serif;color:{BRAND_TEXT};">

  <!-- ═══ HEADER BAR ═══ -->
  <tr>
    <td style="padding:24px 32px;border-bottom:1px solid {BRAND_BORDER};">
      <table role="presentation" width="100%" cellspacing="0" cellpadding="0" border="0">
        <tr>
          <td align="left" valign="middle" style="vertical-align:middle;">
            <!-- Logo swaps based on prefers-color-scheme: dark variant for dark mode,
                 light variant (default) for light mode. <picture> is supported in
                 Gmail web/iOS/Android, Apple Mail, modern Outlook. Clients without
                 <picture> support fall back to the <img> default (light variant). -->
            <picture>
              <source media="(prefers-color-scheme: dark)" srcset="{LOGO_DARK_1X} 1x, {LOGO_DARK_2X} 2x" />
              <img src="{LOGO_LIGHT_2X}" srcset="{LOGO_LIGHT_1X} 1x, {LOGO_LIGHT_2X} 2x" alt="Client Compass" width="160" height="67" style="display:block;border:0;outline:none;text-decoration:none;width:160px;height:67px;" />
            </picture>
          </td>
          <td align="right" valign="middle" style="vertical-align:middle;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;font-size:13px;color:{BRAND_TEXT_LIGHT};">
            <a href="{SITE_URL}" style="color:{BRAND_TEXT_LIGHT};text-decoration:none;">clientcompass.co.za</a>
          </td>
        </tr>
      </table>
    </td>
  </tr>

  <!-- ═══ HERO BLOCK ═══ -->
  <tr>
    <td style="padding:40px 32px 12px 32px;">
      <p style="margin:0 0 10px 0;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;font-size:12px;font-weight:600;color:{BRAND_PRIMARY};letter-spacing:0.08em;text-transform:uppercase;">
        Your Website · Free Preview
      </p>
      <h1 style="margin:0;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,'Helvetica Neue',Arial,sans-serif;font-size:24px;font-weight:700;line-height:1.3;color:{BRAND_TEXT};mso-line-height-rule:exactly;">
        Hi {greeting_name} — quick look at your website
      </h1>
    </td>
  </tr>

  <!-- ═══ BODY INTRO ═══ -->
  <tr>
    <td style="padding:12px 32px 24px 32px;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;font-size:15px;line-height:1.6;color:{BRAND_TEXT_MUTED};">
      <p style="margin:0;">
        I research SA trades &amp; service businesses and ran a quick audit on
        <strong style="color:{BRAND_TEXT};">{business_name}</strong>'s site. Here's what the numbers showed:
      </p>
    </td>
  </tr>

  <!-- ═══ STAT CARD ═══ -->
  <tr>
    <td style="padding:0 32px 24px 32px;">
      <table role="presentation" width="100%" cellspacing="0" cellpadding="0" border="0" style="background:#f8fafc;border-left:4px solid {stat_card_border};border-radius:8px;overflow:hidden;">
        <tr>
          <td style="padding:18px 22px;">
            <p style="margin:0 0 10px 0;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;font-size:11px;font-weight:600;color:{BRAND_TEXT_LIGHT};letter-spacing:0.08em;text-transform:uppercase;">
              What the audit found
            </p>
            <table role="presentation" width="100%" cellspacing="0" cellpadding="0" border="0" style="font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;">
{stat_rows_html}
            </table>
          </td>
        </tr>
      </table>
    </td>
  </tr>

  <!-- ═══ BODY CONTEXT — pain in seconds, not percentiles ═══ -->
  <tr>
    <td style="padding:0 32px 20px 32px;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;font-size:15px;line-height:1.6;color:{BRAND_TEXT_MUTED};">
      <p style="margin:0 0 14px 0;">
        {pain_sentence}
      </p>
      <p style="margin:0;">
        I built a quick preview of what your new website could look like — fast on phones, {platform_pitch}:
      </p>
    </td>
  </tr>

  <!-- ═══ PREVIEW BLOCK ═══ -->
  <tr>
    <td style="padding:0 32px 8px 32px;">
      <table role="presentation" width="100%" cellspacing="0" cellpadding="0" border="0" style="background:#0f172a;border-radius:8px;overflow:hidden;">
        <tr>
          <td align="center" style="padding:16px;">
            {screenshot_html.replace("__MOCKUP_URL__", mockup_url)}
          </td>
        </tr>
      </table>
      <p style="margin:8px 0 0 0;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;font-size:12px;color:{BRAND_TEXT_LIGHT};text-align:center;">
        Can't see the preview?
        <a href="{mockup_url}" target="_blank" style="color:{BRAND_PRIMARY};text-decoration:underline;">View the live version →</a>
      </p>
      <p style="margin:12px 0 0 0;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;font-size:13px;line-height:1.6;color:{BRAND_TEXT_MUTED};text-align:center;">
        <strong style="color:{BRAND_TEXT};">Full website report attached</strong> &mdash; I&rsquo;ve attached a 1-page report you can share with a partner or refer back to later.
      </p>
      <p style="margin:10px 0 0 0;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;font-size:12px;color:{BRAND_TEXT_LIGHT};text-align:center;">
        Fast on 3G too &mdash; loads quickly without eating your customers&rsquo; data.
      </p>
    </td>
  </tr>

  <!-- ═══ OFFER / PRICING CARD ═══ -->
  <tr>
    <td style="padding:8px 32px 24px 32px;">
      <table role="presentation" width="100%" cellspacing="0" cellpadding="0" border="0" style="background:#f0fdf4;border-left:4px solid {BRAND_PRIMARY};border-radius:8px;overflow:hidden;">
        <tr>
          <td style="padding:18px 22px;">
            <p style="margin:0 0 6px 0;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;font-size:11px;font-weight:600;color:{BRAND_PRIMARY_DARK};letter-spacing:0.08em;text-transform:uppercase;">
              The rebuild price
            </p>
            <p style="margin:0 0 10px 0;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;font-size:17px;font-weight:700;color:{BRAND_TEXT};line-height:1.3;">
              {offer['headline']}
            </p>
            <p style="margin:0 0 10px 0;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;font-size:14px;line-height:1.55;color:{BRAND_TEXT_MUTED};">
              {offer['body']}
            </p>
            <p style="margin:0;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;font-size:14px;line-height:1.55;color:{BRAND_TEXT_MUTED};">
              {offer['closing']}
            </p>
          </td>
        </tr>
      </table>
    </td>
  </tr>

  <!-- ═══ CTA BUTTON ═══ -->
  <tr>
    <td align="center" style="padding:8px 32px 8px 32px;">
      <table role="presentation" cellspacing="0" cellpadding="0" border="0">
        <tr>
          <td align="center" bgcolor="{BRAND_PRIMARY}" style="background:{BRAND_PRIMARY};border-radius:8px;">
            <a href="{mockup_url}" target="_blank" style="display:inline-block;padding:14px 32px;color:#ffffff;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;font-size:15px;font-weight:600;text-decoration:none;border-radius:8px;line-height:1.2;">
              Click here to see your preview
            </a>
          </td>
        </tr>
      </table>
      <p style="margin:14px 0 0 0;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;font-size:13px;color:{BRAND_TEXT_LIGHT};">
        No pressure &mdash; if you're happy with your current site, just ignore this.
      </p>
    </td>
  </tr>

  <!-- ═══ SIGNATURE ═══ -->
  <tr>
    <td style="padding:32px;border-top:1px solid {BRAND_BORDER};">
      <table role="presentation" cellspacing="0" cellpadding="0" border="0">
        <tr>
          <td valign="top" style="font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;font-size:14px;line-height:1.5;color:{BRAND_TEXT_MUTED};">
            <p style="margin:0 0 4px 0;font-weight:600;color:{BRAND_TEXT};">{SENDER_NAME}</p>
            <p style="margin:0 0 4px 0;color:{BRAND_TEXT_LIGHT};font-size:13px;">
              Solo founder, building modern sites for SA trades &amp; service businesses from {COMPANY_CITY}
            </p>
            <p style="margin:0;font-size:13px;">
              <a href="https://wa.me/{WHATSAPP_NUMBER}" style="color:{BRAND_PRIMARY};text-decoration:none;font-weight:600;">💬 WhatsApp me</a>
              <span style="color:{BRAND_BORDER};margin:0 8px;">·</span>
              <a href="tel:+{WHATSAPP_NUMBER}" style="color:{BRAND_TEXT_LIGHT};text-decoration:none;">{PHONE_DISPLAY}</a>
              <span style="color:{BRAND_BORDER};margin:0 8px;">·</span>
              <a href="{SITE_URL}" style="color:{BRAND_TEXT_LIGHT};text-decoration:none;">clientcompass.co.za</a>
            </p>
          </td>
        </tr>
      </table>
    </td>
  </tr>

  <!-- ═══ FOOTER — physical address (ECTA s.45), improved consent basis ═══ -->
  <tr>
    <td style="padding:24px 32px;background:#f8fafc;border-top:1px solid {BRAND_BORDER};font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;font-size:12px;line-height:1.6;color:{BRAND_TEXT_LIGHT};">
      <p style="margin:0 0 6px 0;font-weight:600;color:{BRAND_TEXT_MUTED};">
        {_esc(COMPANY_NAME)}
      </p>
      <p style="margin:0 0 4px 0;">
        <a href="mailto:{COMPANY_EMAIL}" style="color:{BRAND_TEXT_MUTED};text-decoration:underline;">{COMPANY_EMAIL}</a>
        <span style="color:{BRAND_BORDER};margin:0 6px;">·</span>
        <span>📍 {_esc(COMPANY_CITY)}</span>
      </p>
      {registration_paragraph}
      <p style="margin:0 0 12px 0;font-size:11px;color:{BRAND_TEXT_LIGHT};">
        You received this because your business is listed publicly and we thought a faster website would help. We only use your details to send this one email &mdash; nothing else.
      </p>
      <p style="margin:0;font-size:11px;color:{BRAND_TEXT_LIGHT};">
        <a href="{unsubscribe_url}" style="color:{BRAND_TEXT_LIGHT};text-decoration:underline;">Unsubscribe</a>
      </p>
    </td>
  </tr>

</table>

<!--[if mso | IE]>
</td></tr></table>
<![endif]-->

</td>
</tr>
</table>

</body>
</html>'''

    return EmailContent(
        subject=subject,
        body_text=body_text,
        body_html=body_html,
        template_key="web_revamp_modern",
        attachments={
            "pdf_path": getattr(lead, "web_audit_pdf_path", None),
            "mockup_url": mockup_url,
        },
    )


def encode_screenshot_as_data_uri(image_bytes: bytes, mime_type: str = "image/jpeg") -> str:
    """Encode raw image bytes as a base64 data URI for inline embedding.

    Returns: "data:image/jpeg;base64,/9j/4AAQ..."
    """
    if not image_bytes:
        return None
    b64 = base64.b64encode(image_bytes).decode("ascii")
    return f"data:{mime_type};base64,{b64}"


def image_size_warning(image_bytes: bytes, max_kb: int = 80) -> bool:
    """Return True if the image is small enough to inline safely.

    Gmail clips total message size at 102KB. With ~15KB of HTML+text body
    and other content, an inline image should stay under ~80KB.
    """
    return len(image_bytes) <= (max_kb * 1024)