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

from .email_builder import (
    SENDER_NAME,
    PLATFORM_DISPLAY_MAP,
    EmailContent,
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
WHATSAPP_NUMBER = "27740940550"  # SA format, no '+'
PHONE_DISPLAY = "+27 74 094 0550"

COMPANY_NAME = "Client Compass Digital Solutions Pty (Ltd)"
COMPANY_EMAIL = "info@clientcompass.co.za"


def _esc(text: str) -> str:
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


def render_modern_web_revamp(
    lead,
    screenshot_ref: str | None = None,
    base_url: str = "https://cc-leadgen.clientcompass.co.za",
) -> EmailContent:
    """Render the modern email.

    Args:
        screenshot_ref: Either a `data:image/...;base64,...` URI (inline) or
            an absolute `https://...` URL (publicly hosted). When provided,
            the screenshot is embedded in the dark preview block.
    """
    """Render a modern, branded email for a mockup-approved web revamp lead.

    Args:
        lead: Lead model instance (or shim) with attributes:
            business_name, owner_name, email, website_platform,
            pagespeed_mobile, web_pitch_score, mockup_url
        screenshot_data_uri: Optional base64 data URI of a mockup screenshot
            (e.g. "data:image/jpeg;base64,/9j/4AAQ..."). If provided, embedded
            inline. If None, the email shows a fallback link to the live URL.

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

    mobile_score = getattr(lead, "pagespeed_mobile", None)
    pitch_score = getattr(lead, "web_pitch_score", 0) or 0
    mockup_url = getattr(lead, "mockup_url", None) or "#"

    unsubscribe_url = f"{base_url}/unsubscribe?token={getattr(lead, 'id', '')}"

    # ── Stat card rows (smart-picked; replaces the fixed 3-row card from
    # the 2026-07-14 build that included "Opportunity score", an internal
    # leadgen ranking metric that didn't communicate anything actionable
    # to the prospect). See app/utils/email_metrics.py for the picker. ──
    stat_rows = pick_stat_rows(lead)

    # ── Plain-text fallback (for text-only email clients) ────────────────
    audit_bullets = "\n".join(f"  {line}" for line in pick_stat_rows_text(lead))
    body_text = (
        f"Hi {greeting_name},\n\n"
        f"I came across {business_name} while researching businesses in the area "
        f"and ran a quick audit on your website.\n\n"
        f"What the audit found:\n"
        f"{audit_bullets}\n\n"
        f"Slow mobile performance means customers on phones are likely leaving "
        f"before your page finishes loading — which directly costs you quote requests.\n\n"
        f"I built a quick mockup of what your new site could look like:\n"
        f"{mockup_url}\n\n"
        f"No pressure — if you're happy with your current site, just ignore this.\n\n"
        f"— JJ Jacobs\n"
        f"Client Compass\n"
        f"WhatsApp: {PHONE_DISPLAY}\n"
    )

    # ── Subject ──────────────────────────────────────────────────────────
    subject = f"Your website audit for {business_name} — preview inside"

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

    # ── Mockup screenshot block ──────────────────────────────────────────
    # screenshot_ref is either:
    #   - a data:image URI (inline base64) — works in Apple Mail, Outlook, but
    #     stripped by Gmail. Use as fallback when upload fails.
    #   - an absolute https URL (uploaded to clientcompass.co.za) — works in
    #     every email client including Gmail. Preferred.
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
                  ▷ View your free mockup →
                </a>
              </td>
            </tr>
          </table>
'''

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
  Mobile {preheader_value} · Free preview of your new website inside.
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
            <img src="{LOGO_URL}" srcset="{LOGO_URL_1X} 1x, {LOGO_URL} 2x" alt="Client Compass" width="160" height="67" style="display:block;border:0;outline:none;text-decoration:none;width:160px;height:67px;" />
          </td>
          <td align="right" valign="middle" style="vertical-align:middle;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;font-size:13px;color:{BRAND_TEXT_LIGHT};">
            <a href="{SITE_URL}" style="color:{BRAND_TEXT_LIGHT};text-decoration:none;">clientcompass.co.za</a>
          </td>
        </tr>
      </table>
    </td>
  </tr>

  <!-- ═══ HERO INTRO ═══ -->
  <tr>
    <td style="padding:40px 32px 12px 32px;">
      <p style="margin:0 0 10px 0;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;font-size:12px;font-weight:600;color:{BRAND_PRIMARY};letter-spacing:0.08em;text-transform:uppercase;">
        Website Audit · Free Preview
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
        I came across <strong style="color:{BRAND_TEXT};">{business_name}</strong> while researching businesses in the area and ran a quick audit on your site. The short version — there's a strong opportunity here.
      </p>
    </td>
  </tr>

  <!-- ═══ STAT CARD ═══ -->
  <tr>
    <td style="padding:0 32px 24px 32px;">
      <table role="presentation" width="100%" cellspacing="0" cellpadding="0" border="0" style="background:#f8fafc;border-left:4px solid {BRAND_PRIMARY};border-radius:8px;overflow:hidden;">
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

  <!-- ═══ BODY CONTEXT ═══ -->
  <tr>
    <td style="padding:0 32px 20px 32px;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;font-size:15px;line-height:1.6;color:{BRAND_TEXT_MUTED};">
      <p style="margin:0 0 14px 0;">
        Mobile scores below 50 typically mean customers on phones leave before your page finishes loading — which directly costs you quote requests.
      </p>
      <p style="margin:0;">
        I built a quick mockup of what your new site could look like — fast, mobile-first, no Wix watermark:
      </p>
    </td>
  </tr>

  <!-- ═══ MOCKUP PREVIEW BLOCK ═══ -->
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
    </td>
  </tr>

  <!-- ═══ CTA BUTTON ═══ -->
  <tr>
    <td align="center" style="padding:24px 32px 8px 32px;">
      <table role="presentation" cellspacing="0" cellpadding="0" border="0">
        <tr>
          <td align="center" bgcolor="{BRAND_PRIMARY}" style="background:{BRAND_PRIMARY};border-radius:8px;">
            <a href="{mockup_url}" target="_blank" style="display:inline-block;padding:14px 32px;color:#ffffff;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;font-size:15px;font-weight:600;text-decoration:none;border-radius:8px;line-height:1.2;">
              See your website mockup →
            </a>
          </td>
        </tr>
      </table>
      <p style="margin:14px 0 0 0;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;font-size:13px;color:{BRAND_TEXT_LIGHT};">
        No pressure — if you're happy with your current site, just ignore this.
      </p>
    </td>
  </tr>

  <!-- ═══ SIGNATURE ═══ -->
  <tr>
    <td style="padding:32px;border-top:1px solid {BRAND_BORDER};">
      <table role="presentation" cellspacing="0" cellpadding="0" border="0">
        <tr>
          <td valign="top" style="padding-right:16px;">
            <img src="{FAVICON_URL}" alt="" width="44" height="44" style="display:block;border-radius:50%;border:0;outline:none;width:44px;height:44px;" />
          </td>
          <td valign="top" style="font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;font-size:14px;line-height:1.5;color:{BRAND_TEXT_MUTED};">
            <p style="margin:0 0 4px 0;font-weight:600;color:{BRAND_TEXT};">JJ Jacobs</p>
            <p style="margin:0 0 10px 0;color:{BRAND_TEXT_LIGHT};font-size:13px;">
              Client Compass · We build &amp; host modern websites for SA small businesses
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

  <!-- ═══ FOOTER (company name + email; physical address removed per operator directive 2026-07-15) ═══ -->
  <tr>
    <td style="padding:24px 32px;background:#f8fafc;border-top:1px solid {BRAND_BORDER};font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;font-size:12px;line-height:1.6;color:{BRAND_TEXT_LIGHT};">
      <p style="margin:0 0 6px 0;font-weight:600;color:{BRAND_TEXT_MUTED};">
        {_esc(COMPANY_NAME)}
      </p>
      <p style="margin:0 0 12px 0;">
        <a href="mailto:{COMPANY_EMAIL}" style="color:{BRAND_TEXT_MUTED};text-decoration:underline;">{COMPANY_EMAIL}</a>
      </p>
      <p style="margin:0;font-size:11px;color:{BRAND_TEXT_LIGHT};">
        You're receiving this because we thought Client Compass could help your business.&nbsp;
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