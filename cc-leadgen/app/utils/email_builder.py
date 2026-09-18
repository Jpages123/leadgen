"""Email content builder — extracted from app.workers.outreach so it can be
called by both the auto-send task and the new draft-generation task.

Public API:
  render_email_for_lead(lead, base_url="...") -> EmailContent
    Returns EmailContent(subject, body_text, body_html, template_key, attachments)
    ready to be either:
      - turned into a draft row (admin_crm.email_drafts)
      - wrapped in a MIME message and sent via SMTP

The mockup URL injection logic (for mockup_status='approved' leads) is
included here so both paths produce identical content.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from dataclasses import dataclass
from jinja2 import Template

import jwt

from app.config import get_settings

SENDER_NAME = "JJ Jacobs"

# CIPC / SARS registration (legitimacy signal). Leave empty if not set —
# the footer line is rendered conditionally, so empty values produce an
# empty string (no placeholder text shown).
#
# Fill in your real numbers. Anyone can verify these on:
#   https://www.cipc.co.za/  (company registration lookup)
#   https://www.sars.gov.za/ (VAT verification)
CIPC_REG_NUMBER = "2026/055613/07"   # CIPC — Client Compass Digital Solutions (Pty) Ltd
VAT_NUMBER = ""                       # not VAT registered yet — leave empty


def _company_registration_segment() -> str:
    """Return the 'Reg. No. X · VAT No. Y' segment, or empty string.

    Used by the three web_revamp Jinja templates. Each segment is only
    included if its constant is set, so this works whether you have one,
    both, or neither.
    """
    parts = []
    if CIPC_REG_NUMBER:
        parts.append(f"Reg. No. {CIPC_REG_NUMBER}")
    if VAT_NUMBER:
        parts.append(f"VAT No. {VAT_NUMBER}")
    return " · ".join(parts)

EMAIL_TEMPLATES = {
    "hair_beauty": """
Hi {{ owner_name or 'there' }},

Saw {{ business_name }} on Yep Mall — looks like you're doing great work.

Quick question: do clients book or message you through WhatsApp? If so, how are you managing it when you're with a client?

Most hair and beauty salons I talk to say WhatsApp is their busiest channel — but also the hardest to keep up with.

We built a tool specifically for that — I can show you how in 10 minutes if you're curious.

Reply YES to get a free walkthrough on WhatsApp, or just reply with any questions.

Talk soon,
{{ sender_name }}
Client Compass
""",
    "cleaning": """
Hi {{ owner_name or 'there' }},

Found {{ business_name }} on Yep Mall — great to see you offering cleaning services.

Quick question: how do you handle enquiries when you're already with a client? Do clients WhatsApp you, and if so — does it get overwhelming?

Most solo cleaners I speak to say WhatsApp is their #1 channel for getting bookings, but it's also where they lose leads because they can't reply fast enough.

We built a tool for that — it keeps your WhatsApp organized even when you can't check it.

Interested in seeing how it works? Reply YES for a free 10-minute walkthrough on WhatsApp.

Or just reply with any questions — happy to help either way.

{{ sender_name }}
Client Compass
""",
    "default": """
Hi {{ owner_name or 'there' }},

I came across {{ business_name }} and wanted to reach out.

Do you use WhatsApp for communicating with clients or taking bookings? If so, how do you manage it when you're busy?

We built a simple tool that helps businesses like yours keep WhatsApp organized — so you never miss a enquiry even when you're hands-full.

Reply YES if you'd like to see a quick demo on WhatsApp.

{{ sender_name }}
Client Compass
""",
    "web_revamp": """Hi {{ owner_name or 'there' }},

I ran a quick check on {{ business_name }}'s website and put together a short report — I've attached it to this email.

The main findings:
- Platform: {{ platform_display }}
- Mobile performance score: {{ mobile_score }}/100 (Google's threshold is 90+)
- This likely means customers on phones are leaving before the page loads

I build fast, modern websites for South African trades and service businesses. The rebuild price is R5,000 once-off — a brand new website is R8,000, but if you've already got a site, a rebuild is the smarter move (I reuse your domain, content, photos and branding, so you only pay for the part that actually needs changing). Most rebuilds are done within 7 working days. Plus R395/month for hosting and a monthly check-up on your site. All prices incl. VAT. Pay via PayFast (instant EFT, card or SnapScan) when you accept the proposal.

I only do 3 website rebuilds each month, so each one gets my full attention. No monthly Wix or WordPress fees. You own the site outright. Fast on 3G too — loads quickly without eating your customers' data.

Would you be open to a quick 10-minute call to see if it's a fit?

{{ sender_name }}
Client Compass
clientcompass.co.za | +27 74 094 0550 | Cape Town, Western Cape
""",
    "web_revamp_2": """Hi {{ owner_name or 'there' }},

Just following up on the website check I sent over for {{ business_name }} a few days ago — not sure if it made it past your inbox.

The short version: your mobile performance score is {{ mobile_score }}/100, and most visitors on their phones are leaving before the page even finishes loading.{% if mockup_url %} I put together a quick preview of what a rebuilt site could look like: {{ mockup_url }}{% endif %}

Quick note — I only do 3 website rebuilds each month, most within 7 working days. R5,000 once-off for the rebuild + R395/month for hosting and a monthly check-up (a brand new website is R8,000 — but if you've already got a site, a rebuild is the smarter move). All prices incl. VAT, paid via PayFast. If you want one of this month's spots, just reply or send me a WhatsApp and I'll send a short proposal.

Happy to walk you through it on a quick call, or just reply here with any questions.

{{ sender_name }}
Client Compass
clientcompass.co.za | +27 74 094 0550 | Cape Town, Western Cape
""",
    "web_revamp_3": """Hi {{ owner_name or 'there' }},

I haven't heard back, so I'll keep this short — I'll close out {{ business_name }}'s file after this unless something changes.

If a faster, modern site (owned outright, no monthly fees) becomes a priority down the line, just reply to this email and we can pick it back up.{% if mockup_url %} The preview I built is still here: {{ mockup_url }}{% endif %}

One last thing — I only do 3 website rebuilds each month, and this month's spots are still open. The rebuild price stays at R5,000, most done within 7 working days. A brand new website is R8,000, but a rebuild is the smarter move when you've already got a site. Plus R395/month for hosting and a monthly check-up, all incl. VAT, via PayFast.

All the best,
{{ sender_name }}
Client Compass
clientcompass.co.za | +27 74 094 0550 | Cape Town, Western Cape
""",
}

HTML_TEMPLATES = {
    "hair_beauty": """
<!DOCTYPE html>
<html><body style="font-family: Arial, sans-serif; max-width: 600px; margin: auto; padding: 20px;">
<p>Hi {{ owner_name or 'there' }},</p>
<p>Saw <strong>{{ business_name }}</strong> on Yep Mall — looks like you're doing great work.</p>
<p>Quick question: do clients book or message you through WhatsApp? If so, how are you managing it when you're with a client?</p>
<p>Most hair and beauty salons I talk to say WhatsApp is their busiest channel — but also the hardest to keep up with.</p>
<p>We built a tool specifically for that. I can show you how in 10 minutes if you're curious.</p>
<p><a href="https://clientcompass.co.za" style="background:#3b82f6;color:white;padding:10px 20px;text-decoration:none;border-radius:5px;">Reply YES for a free WhatsApp demo</a></p>
<p>Or just reply with any questions — happy to help.</p>
<p>Talk soon,<br>{{ sender_name }}<br>Client Compass<br><a href="https://clientcompass.co.za">clientcompass.co.za</a></p>
<hr style="border:none;border-top:1px solid #eee;margin:20px 0;">
<p style="font-size:12px;color:#888;">You're receiving this because you run a business in South Africa and we think Client Compass could help.
<a href="{{ unsubscribe_url }}">Unsubscribe</a> — we respect your time.</p>
</body></html>
""",
    "cleaning": """
<!DOCTYPE html>
<html><body style="font-family: Arial, sans-serif; max-width: 600px; margin: auto; padding: 20px;">
<p>Hi {{ owner_name or 'there' }},</p>
<p>Found <strong>{{ business_name }}</strong> on Yep Mall — great to see you offering cleaning services.</p>
<p>Quick question: how do you handle enquiries when you're already with a client? Do clients WhatsApp you, and if so — does it get overwhelming?</p>
<p>Most solo cleaners I speak to say WhatsApp is their #1 channel for getting bookings, but it's also where they lose leads.</p>
<p>We built a tool for that. Interested in seeing how it works?</p>
<p><a href="https://clientcompass.co.za" style="background:#3b82f6;color:white;padding:10px 20px;text-decoration:none;border-radius:5px;">Reply YES for a free WhatsApp demo</a></p>
<p>{{ sender_name }}<br>Client Compass<br><a href="https://clientcompass.co.za">clientcompass.co.za</a></p>
<hr style="border:none;border-top:1px solid #eee;margin:20px 0;">
<p style="font-size:12px;color:#888;">You're receiving this because you run a business in South Africa.
<a href="{{ unsubscribe_url }}">Unsubscribe</a> — we respect your time.</p>
</body></html>
""",
    "default": """
<!DOCTYPE html>
<html><body style="font-family: Arial, sans-serif; max-width: 600px; margin: auto; padding: 20px;">
<p>Hi {{ owner_name or 'there' }},</p>
<p>I came across <strong>{{ business_name }}</strong> and wanted to reach out.</p>
<p>Do you use WhatsApp for communicating with clients or taking bookings? If so, how do you manage it when you're busy?</p>
<p>We built a simple tool that helps businesses keep WhatsApp organized.</p>
<p><a href="https://clientcompass.co.za" style="background:#3b82f6;color:white;padding:10px 20px;text-decoration:none;border-radius:5px;">Reply YES for a free demo</a></p>
<p>{{ sender_name }}<br>Client Compass<br><a href="https://clientcompass.co.za">clientcompass.co.za</a></p>
<hr style="border:none;border-top:1px solid #eee;margin:20px 0;">
<p style="font-size:12px;color:#888;"><a href="{{ unsubscribe_url }}">Unsubscribe</a></p>
</body></html>
""",
    "web_revamp": """<!DOCTYPE html><html><body style="font-family:Arial,sans-serif;max-width:600px;margin:0 auto;color:#333;">
<p>Hi {{ owner_name or 'there' }},</p>
<p>I ran a quick check on <strong>{{ business_name }}</strong>'s website and put together a short report — I've attached it to this email.</p>
<table style="background:#f8f9fa;border-left:4px solid #1a4d5c;padding:16px;margin:16px 0;width:100%;border-radius:4px;">
  <tr><td><strong>Platform:</strong> {{ platform_display }}</td></tr>
  <tr><td><strong>Mobile performance:</strong> {{ mobile_score }}/100 <span style="color:#dc3545;">(Google threshold: 90+)</span></td></tr>
</table>
<p>Slow mobile performance means customers on phones are likely leaving before the page finishes loading — which directly costs you enquiries.</p>
<p><strong>The rebuild price:</strong> R5,000 once-off — a brand new website is R8,000, but if you've already got a site, a rebuild is the smarter move (I reuse your domain, content, photos and branding, so you only pay for the part that actually needs changing). Most rebuilds are done within 7 working days. Plus R395/month for hosting and a monthly check-up. All prices incl. VAT. Pay via PayFast (instant EFT, card or SnapScan) when you accept the proposal.</p>
<p>I only do 3 website rebuilds each month, so each one gets my full attention. You own the site outright — no monthly Wix or WordPress fees. Fast on 3G too.</p>
<p>Would you be open to a quick 10-minute call to see if it's a fit?</p>
<p>{{ sender_name }}<br>Client Compass<br>
<a href="https://clientcompass.co.za">clientcompass.co.za</a> | +27 74 094 0550 | Cape Town, Western Cape</p>
<hr style="border:none;border-top:1px solid #eee;margin:20px 0;">
<p style="font-size:12px;color:#888;">Client Compass Digital Solutions Pty (Ltd) · Cape Town, Western Cape{% if company_registration %} · {{ company_registration|safe }}{% endif %} · <a href="{{ unsubscribe_url }}">Unsubscribe</a></p>
</body></html>
""",
    "web_revamp_2": """<!DOCTYPE html><html><body style="font-family:Arial,sans-serif;max-width:600px;margin:0 auto;color:#333;">
<p>Hi {{ owner_name or 'there' }},</p>
<p>Just following up on the website check I sent over for <strong>{{ business_name }}</strong> a few days ago — not sure if it made it past your inbox.</p>
<p>The short version: your mobile performance score is <strong>{{ mobile_score }}/100</strong>, and most visitors on their phones are leaving before the page even finishes loading.</p>
{% if mockup_url %}<p>I put together a quick preview of what a rebuilt site could look like: <a href="{{ mockup_url }}">{{ mockup_url }}</a></p>{% endif %}
<p><strong>Quick note:</strong> I only do 3 website rebuilds each month, most within 7 working days. R5,000 once-off for the rebuild + R395/month for hosting and a monthly check-up. A brand new website is R8,000 — but if you've already got a site, a rebuild is the smarter move. All prices incl. VAT, paid via PayFast. Reply or send a WhatsApp if you want one of this month's spots.</p>
<p>Happy to walk you through it on a quick call, or just reply here with any questions.</p>
<p>{{ sender_name }}<br>Client Compass<br>
<a href="https://clientcompass.co.za">clientcompass.co.za</a> | +27 74 094 0550 | Cape Town, Western Cape</p>
<hr style="border:none;border-top:1px solid #eee;margin:20px 0;">
<p style="font-size:12px;color:#888;">Client Compass Digital Solutions Pty (Ltd) · Cape Town, Western Cape{% if company_registration %} · {{ company_registration|safe }}{% endif %} · <a href="{{ unsubscribe_url }}">Unsubscribe</a></p>
</body></html>
""",
    "web_revamp_3": """<!DOCTYPE html><html><body style="font-family:Arial,sans-serif;max-width:600px;margin:0 auto;color:#333;">
<p>Hi {{ owner_name or 'there' }},</p>
<p>I haven't heard back, so I'll keep this short — I'll close out {{ business_name }}'s file after this unless something changes.</p>
<p>If a faster, modern site (owned outright, no monthly fees) becomes a priority down the line, just reply to this email and we can pick it back up.</p>
{% if mockup_url %}<p>The preview I built is still here: <a href="{{ mockup_url }}">{{ mockup_url }}</a></p>{% endif %}
<p>One last thing — I only do 3 website rebuilds each month, and this month's spots are still open. The rebuild price stays at <strong>R5,000</strong>, most done within 7 working days. A brand new website is R8,000, but a rebuild is the smarter move when you've already got a site. Plus R395/month for hosting and a monthly check-up, all incl. VAT, via PayFast.</p>
<p>All the best,<br>{{ sender_name }}<br>Client Compass<br>
<a href="https://clientcompass.co.za">clientcompass.co.za</a> | +27 74 094 0550 | Cape Town, Western Cape</p>
<hr style="border:none;border-top:1px solid #eee;margin:20px 0;">
<p style="font-size:12px;color:#888;">Client Compass Digital Solutions Pty (Ltd) · Cape Town, Western Cape{% if company_registration %} · {{ company_registration|safe }}{% endif %} · <a href="{{ unsubscribe_url }}">Unsubscribe</a></p>
</body></html>
""",
}

PLATFORM_DISPLAY_MAP = {
    "wix": "Wix",
    "wordpress_divi": "WordPress + Divi",
    "wordpress_elementor": "WordPress + Elementor",
    "wordpress_generic": "WordPress",
    "joomla": "Joomla",
    "godaddy": "GoDaddy Website Builder",
    "squarespace": "Squarespace",
    "weebly": "Weebly",
    "static_html": "Static HTML site",
}


@dataclass
class EmailContent:
    """Rendered email content ready to be packaged as a draft or sent."""
    subject: str
    body_text: str
    body_html: str
    template_key: str
    attachments: dict  # {pdf_path: str|None, mockup_url: str|None}
    sequence_step: int = 1


def get_template_key(lead) -> str:
    """Pick the right template for a lead based on category.

    Web revamp template takes priority when:
      - a PDF audit report is attached, OR
      - the lead has an approved mockup (operator review path)
    """
    if getattr(lead, "web_audit_pdf_path", None):
        return "web_revamp"
    if getattr(lead, "mockup_status", None) == "approved" and getattr(lead, "mockup_url", None):
        return "web_revamp"

    category = (getattr(lead, "business_type", "") or "").lower()
    if any(k in category for k in ["hair", "beauty", "nail", "barber", "salon"]):
        return "hair_beauty"
    if any(k in category for k in ["clean", "maid"]):
        return "cleaning"
    return "default"


def build_unsubscribe_url(lead, base_url: str) -> str:
    """Build a signed, time-limited unsubscribe link for a lead.

    ``cc-leadgen.clientcompass.co.za`` has no DNS record — the laptop's
    FastAPI app (where ``/webhook/unsubscribe`` in app/api/webhooks.py
    actually lives) isn't publicly reachable. So the link points at
    ``login.clientcompass.co.za/unsubscribe`` (always-on, VPS-hosted),
    which proxies to the laptop over Tailscale — same pattern as
    ``dispatchSendDraft`` / the PDF proxy in login-portal/routes/admin.js.

    Token matches what the laptop endpoint expects: signed JWT with
    {"lead_id": <uuid str>, "exp": <90 days>}, signed with
    settings.unsubscribe_secret. login-portal doesn't verify the token
    itself — it just forwards it to the laptop verbatim.
    """
    settings = get_settings()
    payload = {
        "lead_id": str(getattr(lead, "id", "")),
        "exp": datetime.now(timezone.utc) + timedelta(days=90),
    }
    token = jwt.encode(payload, settings.unsubscribe_secret, algorithm="HS256")
    return f"{base_url}/unsubscribe?token={token}"


def render_email_for_lead(
    lead, base_url: str = "https://login.clientcompass.co.za", step: int = 1,
) -> EmailContent:
    """Render subject + text + html for a lead using the right template.

    ``step`` selects the touch in the sequence: 1 = initial outreach, 2/3 =
    follow-ups. Follow-up copy (``web_revamp_2``/``web_revamp_3``) only
    exists for the ``web_revamp`` vertical — requesting step 2/3 for any
    other template_key raises, since no other vertical has follow-up copy.

    If the lead has an approved mockup, the mockup URL is appended to the
    body so the prospect can preview their future site.
    """
    template_key = get_template_key(lead)
    render_key = template_key
    if step > 1:
        render_key = f"{template_key}_{step}"
        if render_key not in EMAIL_TEMPLATES:
            raise ValueError(f"No step-{step} follow-up template for '{template_key}'")

    mockup_url = getattr(lead, "mockup_url", None)
    if not (mockup_url and getattr(lead, "mockup_status", "none") == "approved"):
        mockup_url = None

    ctx = {
        "business_name": getattr(lead, "business_name", ""),
        "owner_name": getattr(lead, "owner_name", "") or "",
        "sender_name": SENDER_NAME,
        "unsubscribe_url": build_unsubscribe_url(lead, base_url),
        "platform_display": PLATFORM_DISPLAY_MAP.get(
            getattr(lead, "website_platform", "") or "",
            getattr(lead, "website_platform", "") or "your current platform",
        ),
        "mobile_score": getattr(lead, "pagespeed_mobile", "N/A") or "N/A",
        "web_pitch_score": getattr(lead, "web_pitch_score", 0) or 0,
        "mockup_url": mockup_url,
        "company_registration": _company_registration_segment(),
    }

    body_text = Template(EMAIL_TEMPLATES[render_key]).render(**ctx)
    body_html = Template(HTML_TEMPLATES[render_key]).render(**ctx)

    # Step 1 templates predate the {% if mockup_url %} block, so the mockup
    # preview is spliced in after rendering instead. Step 2/3 templates
    # already render it inline via ctx above.
    if step == 1 and mockup_url:
        body_text = body_text + (
            f"\n\nWe put together a quick preview of what your new site could look like:\n{mockup_url}\n"
        )
        body_html = body_html.replace(
            "</p>\n<p style",
            f'</p>\n<p><strong>We put together a quick preview of what your new site could look like:</strong><br><a href="{mockup_url}">{mockup_url}</a></p>\n<p style',
            1,
        )

    if template_key == "web_revamp":
        subject_prefixes = {
            1: "Website audit for {name} — report attached",
            2: "Following up — website audit for {name}",
            3: "Closing the loop — {name}",
        }
        subject = subject_prefixes[step].format(name=getattr(lead, "business_name", ""))
    else:
        subject = f"Quick question about {getattr(lead, 'business_name', '')}"

    return EmailContent(
        subject=subject,
        body_text=body_text,
        body_html=body_html,
        template_key=template_key,
        attachments={
            # Follow-ups (step > 1) don't re-attach the PDF — it was already
            # sent with step 1; re-sending it just bloats the follow-up.
            "pdf_path": getattr(lead, "web_audit_pdf_path", None) if step == 1 else None,
            "mockup_url": mockup_url,
        },
        sequence_step=step,
    )
