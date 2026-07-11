"""Generate client.ts and brand.ts config files from scraped + LLM data.

These two files are all that need to change to customise the cc-site-template
for a new client. Everything else is template.
"""
from __future__ import annotations

import json
import re
import unicodedata
from pathlib import Path

# ── Vertical defaults ─────────────────────────────────────────────────────────

VERTICAL_DEFAULTS: dict[str, dict] = {
    "trades": {
        "accent": "#e85d04",
        "fontHeading": "Oswald",
        "heroStyle": "trades",
        "stats": [
            {"value": "200+", "label": "Jobs Completed"},
            {"value": "24/7", "label": "Emergency Service"},
            {"value": "5+", "label": "Years Experience"},
            {"value": "4.8★", "label": "Google Rating"},
        ],
        "trustBadges": ["Registered & Insured", "Certificate of Compliance", "No Call-Out Fee"],
    },
    "plumbing": {
        "accent": "#1e9be8",
        "fontHeading": "Oswald",
        "heroStyle": "trades",
        "stats": [
            {"value": "500+", "label": "Jobs Completed"},
            {"value": "24/7", "label": "Emergency Callouts"},
            {"value": "10+", "label": "Years Experience"},
            {"value": "4.9★", "label": "Google Rating"},
        ],
        "trustBadges": ["Registered Plumber", "Certificate of Compliance", "No Call-Out Fee"],
    },
    "electrical": {
        "accent": "#f59e0b",
        "fontHeading": "Oswald",
        "heroStyle": "trades",
        "stats": [
            {"value": "300+", "label": "Installations Done"},
            {"value": "24/7", "label": "Emergency Service"},
            {"value": "8+", "label": "Years Experience"},
            {"value": "4.8★", "label": "Google Rating"},
        ],
        "trustBadges": ["ECSA Registered", "Certificate of Compliance", "Fully Insured"],
    },
    "beauty": {
        "accent": "#b5838d",
        "fontHeading": "Playfair Display",
        "heroStyle": "beauty",
        "stats": [
            {"value": "500+", "label": "Happy Clients"},
            {"value": "5★", "label": "Average Rating"},
            {"value": "8+", "label": "Years Experience"},
            {"value": "15+", "label": "Services Offered"},
        ],
        "trustBadges": ["Qualified Stylist", "Premium Products", "Walk-ins Welcome"],
    },
    "cleaning": {
        "accent": "#0ea5e9",
        "fontHeading": "Oswald",
        "heroStyle": "clean",
        "stats": [
            {"value": "200+", "label": "Cleans Completed"},
            {"value": "100%", "label": "Satisfaction Rate"},
            {"value": "5+", "label": "Years Experience"},
            {"value": "4.9★", "label": "Google Rating"},
        ],
        "trustBadges": ["Fully Insured", "Eco-Friendly Products", "Background Checked"],
    },
    "photography": {
        "accent": "#6366f1",
        "fontHeading": "Playfair Display",
        "heroStyle": "photo",
        "stats": [
            {"value": "300+", "label": "Shoots Completed"},
            {"value": "50+", "label": "Weddings Shot"},
            {"value": "7+", "label": "Years Experience"},
            {"value": "5★", "label": "Google Rating"},
        ],
        "trustBadges": ["Professional Equipment", "Quick Turnaround", "Print-Ready Files"],
    },
    "automotive": {
        "accent": "#ef4444",
        "fontHeading": "Oswald",
        "heroStyle": "trades",
        "stats": [
            {"value": "1000+", "label": "Cars Detailed"},
            {"value": "5★", "label": "Google Rating"},
            {"value": "6+", "label": "Years Experience"},
            {"value": "100%", "label": "Satisfaction Rate"},
        ],
        "trustBadges": ["Premium Products", "Fully Mobile", "Insured Service"],
    },
    "event_planning": {
        "accent": "#c9a84c",
        "fontHeading": "Playfair Display",
        "heroStyle": "photo",
        "heroDark": "#1a1209",
        "surfaceAlt": "#faf8f3",
        "footerDark": "#120d04",
        "fontBody": "Inter",
        "showPoweredBy": True,
        "stats": [
            {"value": "200+", "label": "Events Hosted"},
            {"value": "5★", "label": "Google Rating"},
            {"value": "8+", "label": "Years Experience"},
            {"value": "100%", "label": "Satisfaction Rate"},
        ],
        "trustBadges": ["Fully Insured", "Stress-Free Planning", "End-to-End Service"],
    },
    "construction": {
        "accent": "#d97706",
        "fontHeading": "Oswald",
        "heroStyle": "trades",
        "heroDark": "#0d0a04",
        "surfaceAlt": "#f9f7f3",
        "footerDark": "#080604",
        "fontBody": "Inter",
        "showPoweredBy": True,
        "stats": [
            {"value": "100+", "label": "Projects Completed"},
            {"value": "10+", "label": "Years Experience"},
            {"value": "5★", "label": "Google Rating"},
            {"value": "100%", "label": "Quality Guaranteed"},
        ],
        "trustBadges": ["NHBRC Registered", "Fully Insured", "Free Quotations"],
    },
}

_DEFAULT_VERTICAL = "trades"



_SERVICE_ICONS: dict[str, str] = {
    "emergency": "bolt", "repair": "wrench", "geyser": "flame", "pipe": "droplet",
    "drain": "droplet", "water": "droplet", "leak": "search", "bathroom": "home",
    "install": "tool", "solar": "sun", "electrical": "zap", "board": "zap",
    "commercial": "building", "residential": "home", "maintenance": "tool",
    "hair": "scissors", "photo": "camera", "car": "car", "clean": "sparkles",
    "nail": "sparkles", "gel": "sparkles", "acrylic": "sparkles", "manicure": "sparkles",
    "pedicure": "sparkles", "lash": "eye", "wax": "zap", "brow": "eye",
    "event": "calendar", "wedding": "heart", "decor": "star", "hire": "package",
    "cater": "utensils", "venue": "map-pin", "plan": "clipboard",
    "build": "hammer", "construct": "hammer", "renovate": "hammer", "concrete": "hammer",
    "brick": "layers", "tile": "grid", "roof": "home", "paint": "paintbrush",
}

def _icon_for_service(title: str) -> str:
    t = title.lower()
    for keyword, icon in _SERVICE_ICONS.items():
        if keyword in t:
            return icon
    return "tool"

# ── Slug generation ──────────────────────────────────────────────────────────

def slugify(text: str) -> str:
    """Convert business name to a URL-safe slug."""
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()
    text = text.lower().strip()
    text = re.sub(r"[^\w\s-]", "", text)
    text = re.sub(r"[\s_-]+", "-", text)
    text = re.sub(r"^-+|-+$", "", text)
    return text


# ── TypeScript generators ────────────────────────────────────────────────────

def _ts_str(value: str | None) -> str:
    """Escape a value for a TypeScript string literal."""
    if not value:
        return '""'
    escaped = value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")
    return f'"{escaped}"'


def _ts_null_or_str(value: str | None) -> str:
    return "null" if not value else _ts_str(value)


def generate_client_ts(
    business_name: str,
    tagline: str,
    phone: str | None,
    email: str | None,
    address: str | None,
    domain: str,
    city: str | None,
    services: list[dict],
    vertical: str = _DEFAULT_VERTICAL,
    google_rating: float | None = None,
    google_review_count: int | None = None,
    site_copyright_year: int | None = None,
    facebook_url: str | None = None,
    instagram_url: str | None = None,
    # Personalization (added 2026-07-04): paths to scraped images. The mockup
    # generator copies these into public/images/ before building. Pass an
    # empty list to fall back to the default 4-item gallery template.
    logo_path: str | None = None,
    hero_image_path: str | None = None,
    gallery_paths: list[str] | None = None,
) -> str:
    """Return the full content of src/config/client.ts."""
    defaults = VERTICAL_DEFAULTS.get(vertical, VERTICAL_DEFAULTS[_DEFAULT_VERTICAL])

    whatsapp = re.sub(r"[^\d]", "", phone or "")

    # Build stats — personalise from real data where available
    stats = list(defaults["stats"])
    # Slot 0: jobs/events/clients — derive from review count
    if google_review_count and google_review_count > 5:
        derived = max(google_review_count * 3, 50)
        if derived >= 1000:
            jobs_val = f"{(derived // 100) * 100}+"
        elif derived >= 100:
            jobs_val = f"{(derived // 50) * 50}+"
        else:
            jobs_val = f"{(derived // 10) * 10}+"
        stats[0] = {"value": jobs_val, "label": stats[0]["label"]}
    # Slot 2: years experience — derive from copyright year
    if site_copyright_year and 2000 <= site_copyright_year <= 2024:
        years = 2026 - site_copyright_year
        stats[2] = {"value": f"{years}+", "label": "Years Experience"}
    # Slot 3: Google rating
    if google_rating and google_review_count:
        stats[3] = {"value": f"{google_rating:.1f}★", "label": f"{google_review_count} Reviews"}

    # Build full address
    full_address = address or (f"{city}, South Africa" if city else "South Africa")

    services = [{**s, "icon": s.get("icon") or _icon_for_service(s.get("title", ""))} for s in services]
    stats_ts = json.dumps(stats, indent=4)
    services_ts = json.dumps(services, indent=4)
    trust_badges_ts = json.dumps(defaults.get("trustBadges", []), indent=4)

    # Gallery: prefer scraped images, fall back to 4-item placeholder
    gallery_ts_lines = []
    if gallery_paths:
        for i, pth in enumerate(gallery_paths[:4], 1):
            ext = pth.split(".")[-1] if "." in pth else "jpg"
            alt = f"{business_name} photo {i}"
            gallery_ts_lines.append(
                f'    {{ src: "/images/gallery/{i}.{ext}", alt: {_ts_str(alt)} }}'
            )
    if not gallery_ts_lines:
        # Default placeholder gallery (template ships these images)
        gallery_ts_lines = [
            f'    {{ src: "/images/gallery/1.jpg", alt: {_ts_str(business_name + " work")} }}',
            f'    {{ src: "/images/gallery/2.jpg", alt: {_ts_str(business_name + " project")} }}',
            f'    {{ src: "/images/gallery/3.jpg", alt: {_ts_str(business_name + " service")} }}',
            f'    {{ src: "/images/gallery/4.jpg", alt: {_ts_str(business_name + " team")} }}',
        ]
    gallery_block = ",\n".join(gallery_ts_lines)

    # Hero image path: the build always writes /images/hero.jpg (Pillow
    # normalises PNG/WebP sources to JPEG). See Session 9 in WEB_REVAMP_ENGINE.md.
    hero_image_ts = "/images/hero.jpg"
    if hero_image_path:
        hero_image_ts = "/images/hero.jpg"

    # Logo path: same convention — the build always writes /images/logo.jpg.
    logo_ts = "null"
    if logo_path:
        logo_ts = '"/images/logo.jpg"'

    return f"""export const client = {{
  name: {_ts_str(business_name)},
  tagline: {_ts_str(tagline)},
  description: {_ts_str(f"{business_name} — {tagline}")},
  phone: {_ts_null_or_str(phone)},
  whatsapp: {_ts_str(whatsapp) if whatsapp else "null"},
  email: {_ts_null_or_str(email)},
  address: {_ts_str(full_address)},
  domain: {_ts_str(domain)},
  googleMapsEmbed: null,
  logo: {logo_ts},
  heroImage: {_ts_str(hero_image_ts)},
  stats: {stats_ts},
  services: {services_ts},
  testimonials: [],
  gallery: [
{gallery_block}
  ],
  trustBadges: {trust_badges_ts},
  social: {{
    facebook: {_ts_null_or_str(facebook_url)},
    instagram: {_ts_null_or_str(instagram_url)},
  }},
  web3FormsKey: "PLACEHOLDER",
  cloudflareAnalyticsToken: null,
  seo: {{
    title: {_ts_str(f"{business_name} | {tagline}")},
    description: {_ts_str(f"{business_name} — {tagline}. Based {full_address}.")},
    ogImage: "/images/og.jpg",
  }},
}} as const;
"""


def generate_brand_ts(
    vertical: str = _DEFAULT_VERTICAL,
    accent_color: str | None = None,
    logo_path: str | None = None,
    hero_image_path: str | None = None,
) -> str:
    """Return the full content of src/config/brand.ts.

    The cc-site-template's components (Nav, Footer, HeroTrades, About) read
    these fields directly. Pass logo_path / hero_image_path to personalize:
      - logo_path  : path under public/ (mockup_generator copies the scraped
                     file to /images/logo.jpg regardless of source format)
      - hero_image_path : same, for /images/hero.jpg
    """
    defaults = VERTICAL_DEFAULTS.get(vertical, VERTICAL_DEFAULTS[_DEFAULT_VERTICAL])
    accent = accent_color or defaults["accent"]
    heading_font = defaults.get("fontHeading", "Oswald")
    hero_style = defaults.get("heroStyle", "trades")
    # The template's Nav/Footer components expect fixed filenames in /images.
    # We always copy to logo.jpg and hero.jpg regardless of source extension,
    # so the template's hardcoded references resolve.
    logo_path_ts = logo_path or "/images/logo.jpg"
    hero_image_ts = hero_image_path or "/images/hero.jpg"

    return f"""export const brand = {{
  heroStyle: {_ts_str(hero_style)},

  // Primary accent colour (buttons, highlights, underlines)
  accent: {_ts_str(accent)},

  // Hero background (dark variants only — ignored for 'photo' style)
  heroDark: {_ts_str(defaults.get("heroDark", "#0a0a0a"))},

  // Section alternating background
  surfaceAlt: {_ts_str(defaults.get("surfaceAlt", "#f9fafb"))},

  // Heading + body fonts (Google Fonts names)
  fontHeading: {_ts_str(heading_font)},
  fontBody: {_ts_str(defaults.get("fontBody", "Inter"))},

  // Path to logo file in /public (mockup generator copies scraped logo here)
  logoPath: {_ts_str(logo_path_ts)},

  // Hero background image in /public (full-bleed photo overlay)
  heroImage: {_ts_str(hero_image_ts)},

  // Footer background colour
  footerDark: {_ts_str(defaults.get("footerDark", "#050505"))},

  // Powered-by badge (set false for Pro tier clients)
  showPoweredBy: {str(defaults.get("showPoweredBy", True)).lower()},
}} as const;
"""


def generate_brand_ts_for_recommendation(
    recommendation: dict,
    vertical: str = _DEFAULT_VERTICAL,
    logo_path: str | None = None,
    hero_image_path: str | None = None,
) -> str:
    """Return brand.ts content using the template_analyzer's recommendation dict.

    The recommendation dict comes from app.utils.template_analyzer.analyze_site_for_mockup.
    It contains all the visual decisions (template, brand colors, layout variant,
    trust signals, etc.) returned by Pi for this specific lead.

    Schema expected (per app/utils/template_analyzer.py):
        {
          "template": "trades" | "creative" | "general",
          "brand_colors": {"primary": ..., "accent": ..., "hero_treatment": ..., "text_contrast": ...},
          "hero_overlay_opacity": int,
          "layout_variant": "gallery-first" | "service-first" | "balanced",
          "gallery_treatment": "masonry-no-caption" | "grid-with-caption" | "clean-grid",
          "services_style": "icon-cards" | "minimal-cards" | "list-style",
          "typography_hint": "serif-preferred" | "sans-preferred",
          "cta_priority": "phone" | "whatsapp" | "contact" | "book",
          "dark_mode": bool,
          "image_watermark": bool,
          "trust_signals": list[str],
        }
    """
    template = recommendation.get("template", vertical)
    colors = recommendation.get("brand_colors", {})
    accent = colors.get("accent") or "#e85d04"
    primary = colors.get("primary") or "#1a1a1a"

    # Template-specific defaults
    if template == "creative":
        heading_font = "Playfair Display"
        body_font = "Inter"
        hero_dark = primary
        surface_alt = "#fafafa" if not recommendation.get("dark_mode") else "#0a0a0a"
        footer_dark = primary if recommendation.get("dark_mode") else "#050505"
    elif template == "trades":
        heading_font = "Oswald"
        body_font = "Inter"
        hero_dark = "#0a0a0a"
        surface_alt = "#f9fafb"
        footer_dark = "#050505"
    else:  # general
        heading_font = "Oswald"
        body_font = "Inter"
        hero_dark = "#1a1a1a"
        surface_alt = "#f7f7f8"
        footer_dark = "#0e0e10"

    # Serif hint pushes to Playfair
    if recommendation.get("typography_hint") == "serif-preferred":
        heading_font = "Playfair Display"

    # Dark mode inverts surface and footer
    if recommendation.get("dark_mode"):
        surface_alt = "#0a0a0a"
        footer_dark = "#000000"

    show_powered = template != "creative"  # Creative: client brand only

    logo_path_ts = logo_path or "/images/logo.jpg"
    hero_image_ts = hero_image_path or "/images/hero.jpg"

    # Trust signals come from the recommendation (template-specific)
    trust_badges_json = json.dumps(recommendation.get("trust_signals", []), indent=2)

    return f"""export const brand = {{
  // Template variant chosen by Pi analysis (drives which hero/gallery render)
  template: {_ts_str(template)},

  // ==== Brand colours (from Pi analysis) ====
  accent: {_ts_str(accent)},
  primary: {_ts_str(primary)},
  textContrast: {_ts_str(colors.get("text_contrast", "light"))},
  heroTreatment: {_ts_str(colors.get("hero_treatment", "image-with-overlay"))},
  heroOverlayOpacity: {int(recommendation.get("hero_overlay_opacity", 55))},

  // ==== Layout decisions ====
  layoutVariant: {_ts_str(recommendation.get("layout_variant", "balanced"))},
  galleryTreatment: {_ts_str(recommendation.get("gallery_treatment", "clean-grid"))},
  servicesStyle: {_ts_str(recommendation.get("services_style", "icon-cards"))},
  ctaPriority: {_ts_str(recommendation.get("cta_priority", "phone"))},

  // ==== Typography ====
  fontHeading: {_ts_str(heading_font)},
  fontBody: {_ts_str(body_font)},

  // ==== Theme surfaces ====
  darkMode: {str(bool(recommendation.get("dark_mode"))).lower()},
  heroDark: {_ts_str(hero_dark)},
  surfaceAlt: {_ts_str(surface_alt)},
  footerDark: {_ts_str(footer_dark)},

  // ==== Image treatment ====
  logoPath: {_ts_str(logo_path_ts)},
  heroImage: {_ts_str(hero_image_ts)},
  imageWatermark: {str(bool(recommendation.get("image_watermark"))).lower()},

  // ==== Template-specific trust signals (from Pi) ====
  trustBadges: {trust_badges_json},

  // ==== Misc ====
  showPoweredBy: {str(show_powered).lower()},
}} as const;
"""


def generate_brand_ts_for_recommendation(
    recommendation: dict,
    vertical: str = _DEFAULT_VERTICAL,
    logo_path: str | None = None,
    hero_image_path: str | None = None,
) -> str:
    """Return brand.ts using the template_analyzer recommendation dict.

    Schema expected:
        {
          "template": "trades" | "creative" | "general",
          "brand_colors": {"primary": ..., "accent": ..., "hero_treatment": ..., "text_contrast": ...},
          "hero_overlay_opacity": int,
          "layout_variant": "gallery-first" | "service-first" | "balanced",
          "gallery_treatment": "masonry-no-caption" | "grid-with-caption" | "clean-grid",
          "services_style": "icon-cards" | "minimal-cards" | "list-style",
          "typography_hint": "serif-preferred" | "sans-preferred",
          "cta_priority": "phone" | "whatsapp" | "contact" | "book",
          "dark_mode": bool,
          "image_watermark": bool,
          "trust_signals": list[str],
        }
    """
    template = recommendation.get("template", vertical)
    colors = recommendation.get("brand_colors", {})
    accent = colors.get("accent") or "#e85d04"
    primary = colors.get("primary") or "#1a1a1a"

    if template == "creative":
        heading_font = "Playfair Display"
        body_font = "Inter"
        hero_dark = primary
        surface_alt = "#fafafa" if not recommendation.get("dark_mode") else "#0a0a0a"
        footer_dark = primary if recommendation.get("dark_mode") else "#050505"
    elif template == "trades":
        heading_font = "Oswald"
        body_font = "Inter"
        hero_dark = "#0a0a0a"
        surface_alt = "#f9fafb"
        footer_dark = "#050505"
    else:
        heading_font = "Oswald"
        body_font = "Inter"
        hero_dark = "#1a1a1a"
        surface_alt = "#f7f7f8"
        footer_dark = "#0e0e10"

    if recommendation.get("typography_hint") == "serif-preferred":
        heading_font = "Playfair Display"

    if recommendation.get("dark_mode"):
        surface_alt = "#0a0a0a"
        footer_dark = "#000000"

    show_powered = template != "creative"

    logo_path_ts = logo_path or "/images/logo.jpg"
    hero_image_ts = hero_image_path or "/images/hero.jpg"

    trust_badges_json = json.dumps(recommendation.get("trust_signals", []), indent=2)

    return f"""export const brand = {{
  template: {_ts_str(template)},
  accent: {_ts_str(accent)},
  primary: {_ts_str(primary)},
  textContrast: {_ts_str(colors.get("text_contrast", "light"))},
  heroTreatment: {_ts_str(colors.get("hero_treatment", "image-with-overlay"))},
  heroOverlayOpacity: {int(recommendation.get("hero_overlay_opacity", 55))},
  layoutVariant: {_ts_str(recommendation.get("layout_variant", "balanced"))},
  galleryTreatment: {_ts_str(recommendation.get("gallery_treatment", "clean-grid"))},
  servicesStyle: {_ts_str(recommendation.get("services_style", "icon-cards"))},
  ctaPriority: {_ts_str(recommendation.get("cta_priority", "phone"))},
  fontHeading: {_ts_str(heading_font)},
  fontBody: {_ts_str(body_font)},
  darkMode: {str(bool(recommendation.get("dark_mode"))).lower()},
  heroDark: {_ts_str(hero_dark)},
  surfaceAlt: {_ts_str(surface_alt)},
  footerDark: {_ts_str(footer_dark)},
  logoPath: {_ts_str(logo_path_ts)},
  heroImage: {_ts_str(hero_image_ts)},
  imageWatermark: {str(bool(recommendation.get("image_watermark"))).lower()},
  trustBadges: {trust_badges_json},
  showPoweredBy: {str(show_powered).lower()},
}} as const;
"""
