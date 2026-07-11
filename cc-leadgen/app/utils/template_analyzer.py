"""Analyze a prospect's site to recommend a mockup template + visual treatment.

Calls the Pi LLM harness (`pi -p <prompt>`) to reason about the site's
visual style based on scraped data. Returns a structured recommendation:
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any

from app.utils.logger import get_logger

log = get_logger(__name__)

TEMPLATE_VERTICAL_DEFAULTS: dict[str, str] = {
    "trades": "trades",
    "plumbing": "trades",
    "electrical": "trades",
    "construction": "trades",
    "cleaning": "trades",
    "automotive": "trades",
    "photography": "creative",
    "event_planning": "creative",
    "beauty": "general",
    "default": "general",
}

VALID_TEMPLATES = frozenset({"trades", "creative", "general"})

FALLBACK_RECOMMENDATION: dict[str, Any] = {
    "template": "general",
    "rationale": "fallback (LLM call failed) - using general template defaults",
    "brand_colors": {
        "primary": "#1a1a1a",
        "accent": "#e85d04",
        "hero_treatment": "image-with-overlay",
        "text_contrast": "light",
    },
    "layout_variant": "balanced",
    "gallery_treatment": "clean-grid",
    "hero_overlay_opacity": 50,
    "services_style": "icon-cards",
    "typography_hint": "sans-preferred",
    "cta_priority": "phone",
    "dark_mode": False,
    "image_watermark": False,
    "trust_signals": ["Fully Insured", "Quality Guaranteed", "Free Quotations"],
    "section_order": ["hero", "trust", "services", "gallery", "about", "testimonials", "contact"],
}

TRUST_SIGNAL_DEFAULTS: dict[str, list[str]] = {
    "trades": [
        "Registered & Insured",
        "Certificate of Compliance",
        "No Call-Out Fee",
        "24/7 Emergency Service",
    ],
    "creative": [
        "Professional Equipment",
        "Quick Turnaround",
        "Print-Ready Files",
        "Featured Work",
    ],
    "general": [
        "Fully Insured",
        "Quality Guaranteed",
        "Free Quotations",
    ],
}

VALID_LAYOUT_VARIANTS = frozenset({"gallery-first", "service-first", "balanced"})
VALID_GALLERY_TREATMENTS = frozenset({"masonry-no-caption", "grid-with-caption", "clean-grid"})
VALID_HERO_TREATMENTS = frozenset({"full-bleed", "split", "image-with-overlay"})
VALID_SERVICES_STYLES = frozenset({"icon-cards", "minimal-cards", "list-style"})
VALID_TYPOGRAPHY_HINTS = frozenset({"serif-preferred", "sans-preferred"})
VALID_CTA_PRIORITIES = frozenset({"phone", "whatsapp", "contact", "book"})


def _resolve_pi() -> str:
    """Locate the pi executable.

    Uses os.access() instead of Path.exists() because the latter raises
    PermissionError on inaccessible paths (e.g. /root owned by another user),
    which would mask the working candidate further down the list.
    """
    found = shutil.which("pi")
    if found:
        return found
    candidates = [
        Path.home() / ".npm-global/bin/pi",
        Path("/usr/local/bin/pi"),
        Path("/root/.npm-global/bin/pi"),
        Path.home() / ".nvm/versions/node/v23.1.0/bin/pi",
    ]
    for candidate in candidates:
        try:
            if os.access(candidate, os.X_OK):
                return str(candidate)
        except OSError:
            continue
    raise FileNotFoundError("Cannot locate pi CLI")


def _extract_json_object(raw: str) -> dict:
    """Robustly extract the first valid JSON object from raw.

    Handles <think>...</think> blocks, trailing text after the JSON,
    and braces that appear inside string values.
    """
    cleaned = re.sub(r"<think>.*?</think>", "", raw, flags=re.DOTALL).strip()
    decoder = json.JSONDecoder()
    for i, ch in enumerate(cleaned):
        if ch != "{":
            continue
        try:
            value, _ = decoder.raw_decode(cleaned[i:])
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            return value
    raise ValueError(
        f"No valid JSON object in template_analyzer output "
        f"(len={len(raw)}, cleaned_len={len(cleaned)})"
    )


def _build_env() -> dict[str, str]:
    env = os.environ.copy()
    pi_path = _resolve_pi()
    pi_bin_dir = str(Path(pi_path).parent)
    npm_global_bin = "/root/.npm-global/bin"
    existing_path = env.get("PATH", "")
    env["PATH"] = ":".join(p for p in [pi_bin_dir, npm_global_bin, existing_path] if p)
    env["TERM"] = "xterm-256color"
    return env


def _build_prompt(
    *,
    business_name: str,
    vertical: str,
    city: str | None,
    website: str | None,
    website_platform: str | None,
    scraped_brand_color: str | None,
    scraped_gallery_count: int,
    pagespeed_mobile: int | None,
    site_copyright_year: int | None,
    google_rating: float | None,
    google_review_count: int | None,
    template_suggestion: str,
) -> str:
    """Compose the prompt sent to pi -p for template recommendation."""
    location = f"in {city}, South Africa" if city else "in South Africa"
    color_hint = (
        f"Extracted from their site's CSS/logo: `{scraped_brand_color}`"
        if scraped_brand_color
        else "No brand color could be extracted from their site."
    )
    speed_hint = (
        f"PageSpeed mobile: {pagespeed_mobile}/100"
        if pagespeed_mobile is not None
        else "PageSpeed unknown."
    )
    age_hint = (
        f"Copyright year in footer: {site_copyright_year}"
        if site_copyright_year
        else "Copyright year not found (site may be very old or new)."
    )
    rating_hint = (
        f"Google rating: {google_rating:.1f}* from {google_review_count} reviews"
        if google_rating and google_review_count
        else "Google rating unavailable."
    )

    return (
        "You are a visual design analyst evaluating a small SA business's existing "
        "website to recommend how their redesigned mockup site should look. "
        "Your recommendation will be turned into a working Astro site for the "
        "business to preview.\n\n"
        f"Business: {business_name}\n"
        f"Vertical: {vertical}\n"
        f"Location: {location}\n"
        f"Website: {website or '(unknown)'}\n"
        f"Platform: {website_platform or 'unknown'}\n"
        f"Brand color: {color_hint}\n"
        f"Images scraped from their site: {scraped_gallery_count}\n"
        f"{speed_hint}  ({age_hint})  {rating_hint}\n\n"
        "There are 3 template variants available:\n\n"
        "1. **trades** - Professional 'calling card' style. Dark hero overlay, bold "
        "headings, prominent service grid, trust badges (license/insurance/24-7), "
        "'Call now' CTA. Best for: plumbing, electrical, construction, cleaning, "
        "automotive.\n\n"
        "2. **creative** - Portfolio-first gallery. Full-bleed hero image, minimal "
        "text overlay, masonry gallery, watermark support, dark mode optional, "
        "'Let's create together' CTA. Best for: photography, videography, event "
        "planning.\n\n"
        "3. **general** - Balanced. Clean grid gallery, medium trust signals, "
        "configurable trust-signals list, flexible layout. Fallback for everything "
        "else.\n\n"
        "TASK:\n"
        "Based on the data above, decide which template will produce the most "
        "impressive, conversion-oriented mockup for this business. The vertical is "
        "a strong signal but not deterministic - if their actual site (e.g. CSS "
        "style, image count, platform age) suggests a different template, override "
        "the vertical default. The goal is a mockup that makes the business owner "
        "say 'wow, that's ME, not a generic template'.\n\n"
        "DECISIONS TO MAKE:\n"
        "1. Which template (trades / creative / general)\n"
        "2. Brand color palette (pick primary + accent hex codes that match their "
        "existing brand, or pick a tasteful default if no color was extracted)\n"
        "3. Hero treatment (full-bleed / split / image-with-overlay)\n"
        "4. Section layout order (which sections come first)\n"
        "5. Whether dark mode suits the brand\n"
        "6. Whether gallery images should have a subtle watermark (only for creative)\n"
        "7. 3 specific trust signals relevant to this template + business\n\n"
        "TRUST SIGNAL EXAMPLES PER TEMPLATE:\n"
        "- trades: 'Registered & Insured', 'Certificate of Compliance', 'No Call-Out Fee', "
        "'24/7 Emergency Service', 'Licensed Plumber'\n"
        "- creative: 'Featured in [Publication]', '500+ sessions', 'Quick turnaround', "
        "'Professional equipment', 'Print-ready files'\n"
        "- general: 'Fully Insured', 'Quality Guaranteed', 'Free Quotations'\n\n"
        "OUTPUT: Return ONLY valid JSON (no markdown, no explanation). The JSON "
        "MUST have EXACTLY these keys and matching types:\n"
        "{\n"
        '  "template": "trades" | "creative" | "general",\n'
        '  "rationale": "1-2 sentences explaining your reasoning in plain English",\n'
        '  "brand_colors": {\n'
        '    "primary": "#RRGGBB",\n'
        '    "accent": "#RRGGBB",\n'
        '    "hero_treatment": "full-bleed" | "split" | "image-with-overlay",\n'
        '    "text_contrast": "light" | "dark"\n'
        "  },\n"
        '  "layout_variant": "gallery-first" | "service-first" | "balanced",\n'
        '  "gallery_treatment": "masonry-no-caption" | "grid-with-caption" | "clean-grid",\n'
        '  "hero_overlay_opacity": <integer 25..70>,\n'
        '  "services_style": "icon-cards" | "minimal-cards" | "list-style",\n'
        '  "typography_hint": "serif-preferred" | "sans-preferred",\n'
        '  "cta_priority": "phone" | "whatsapp" | "contact" | "book",\n'
        '  "dark_mode": true | false,\n'
        '  "image_watermark": true | false,\n'
        '  "trust_signals": ["...", "...", "..."],\n'
        '  "section_order": ["hero", "trust", "services", "gallery", "about", "testimonials", "contact"]\n'
        "}\n\n"
        f"Default template suggested by vertical: {template_suggestion}.\n"
        "You may override it if the site's data supports a different choice.\n\n"
        "Output JSON only - no other text."
    )


def _validate_recommendation(data: dict, vertical: str) -> dict:
    """Coerce LLM output to a clean dict matching the schema."""
    template = data.get("template")
    if not isinstance(template, str) or template not in VALID_TEMPLATES:
        template = TEMPLATE_VERTICAL_DEFAULTS.get(vertical, "general")

    raw_colors = data.get("brand_colors") or {}
    hero_treatment = raw_colors.get("hero_treatment")
    text_contrast = raw_colors.get("text_contrast")
    primary = raw_colors.get("primary")
    accent = raw_colors.get("accent")
    if not isinstance(hero_treatment, str) or hero_treatment not in VALID_HERO_TREATMENTS:
        hero_treatment = "image-with-overlay"
    if text_contrast not in ("light", "dark"):
        text_contrast = "light"
    if not isinstance(primary, str) or not re.match(r"^#[0-9a-fA-F]{6}$", primary):
        primary = "#1a1a1a"
    if not isinstance(accent, str) or not re.match(r"^#[0-9a-fA-F]{6}$", accent):
        accent = "#e85d04"

    layout_variant = data.get("layout_variant")
    if layout_variant not in VALID_LAYOUT_VARIANTS:
        layout_variant = "balanced"

    gallery_treatment = data.get("gallery_treatment")
    if gallery_treatment not in VALID_GALLERY_TREATMENTS:
        gallery_treatment = "clean-grid"

    hero_opacity = data.get("hero_overlay_opacity")
    if not isinstance(hero_opacity, (int, float)) or not (25 <= hero_opacity <= 70):
        hero_opacity = 50
    else:
        hero_opacity = int(hero_opacity)

    services_style = data.get("services_style")
    if services_style not in VALID_SERVICES_STYLES:
        services_style = "icon-cards"

    typography_hint = data.get("typography_hint")
    if typography_hint not in VALID_TYPOGRAPHY_HINTS:
        typography_hint = "sans-preferred"

    cta_priority = data.get("cta_priority")
    if cta_priority not in VALID_CTA_PRIORITIES:
        cta_priority = "phone"

    dark_mode = bool(data.get("dark_mode", False))
    image_watermark = bool(data.get("image_watermark", False))

    # Watermark only makes sense for creative template
    if template != "creative" and image_watermark:
        image_watermark = False

    trust_signals_raw = data.get("trust_signals")
    trust_signals: list[str] = []
    if isinstance(trust_signals_raw, list):
        for sig in trust_signals_raw:
            if not isinstance(sig, str):
                continue
            cleaned = "".join(c for c in sig if c.isprintable() or c == " ").strip()
            if cleaned and cleaned not in trust_signals:
                trust_signals.append(cleaned)
    while len(trust_signals) < 3:
        defaults = TRUST_SIGNAL_DEFAULTS.get(template, TRUST_SIGNAL_DEFAULTS["general"])
        for sig in defaults:
            if sig not in trust_signals:
                trust_signals.append(sig)
                break
    trust_signals = trust_signals[:5]

    valid_sections = {"hero", "trust", "services", "gallery", "about", "testimonials", "contact"}
    section_order_raw = data.get("section_order")
    section_order: list[str] = []
    if isinstance(section_order_raw, list):
        for s in section_order_raw:
            if isinstance(s, str) and s in valid_sections and s not in section_order:
                section_order.append(s)
    while "hero" not in section_order[:1]:
        section_order = ["hero"] + [s for s in section_order if s != "hero"]
    while "contact" not in section_order[-1:]:
        section_order = [s for s in section_order if s != "contact"] + ["contact"]
    for s in ["trust", "services", "gallery", "about", "testimonials"]:
        if s not in section_order:
            section_order.insert(-1, s)

    rationale = data.get("rationale")
    if not isinstance(rationale, str) or not rationale.strip():
        rationale = f"{template} template (default for {vertical})"

    return {
        "template": template,
        "rationale": rationale.strip()[:500],
        "brand_colors": {
            "primary": primary,
            "accent": accent,
            "hero_treatment": hero_treatment,
            "text_contrast": text_contrast,
        },
        "layout_variant": layout_variant,
        "gallery_treatment": gallery_treatment,
        "hero_overlay_opacity": hero_opacity,
        "services_style": services_style,
        "typography_hint": typography_hint,
        "cta_priority": cta_priority,
        "dark_mode": dark_mode,
        "image_watermark": image_watermark,
        "trust_signals": trust_signals,
        "section_order": section_order,
    }


def analyze_site_for_mockup(
    *,
    business_name: str,
    vertical: str,
    city: str | None = None,
    website: str | None = None,
    website_platform: str | None = None,
    scraped_brand_color: str | None = None,
    scraped_gallery_paths: list[str] | None = None,
    pagespeed_mobile: int | None = None,
    site_copyright_year: int | None = None,
    google_rating: float | None = None,
    google_review_count: int | None = None,
    timeout: int = 60,
) -> dict:
    """Call Pi to recommend a template + visual treatment for a prospect."""
    template_suggestion = TEMPLATE_VERTICAL_DEFAULTS.get(vertical, "general")
    gallery_count = len(scraped_gallery_paths) if scraped_gallery_paths else 0

    prompt = _build_prompt(
        business_name=business_name,
        vertical=vertical,
        city=city,
        website=website,
        website_platform=website_platform,
        scraped_brand_color=scraped_brand_color,
        scraped_gallery_count=gallery_count,
        pagespeed_mobile=pagespeed_mobile,
        site_copyright_year=site_copyright_year,
        google_rating=google_rating,
        google_review_count=google_review_count,
        template_suggestion=template_suggestion,
    )

    # Robust cwd detection. In Docker, /app exists AND has the project files.
    # On the host, /app may exist (other apps) but is unrelated - check for project markers.
    is_docker = (
        Path("/app").exists()
        and (Path("/app") / ".venv").exists()
    )
    cwd = "/app" if is_docker else str(
        Path.home() / "installedApps" / "leadgen" / "cc-leadgen"
    )

    try:
        pi_path = _resolve_pi()
        result = subprocess.run(
            [pi_path, "-p", prompt, "--no-tools", "--no-extensions"],
            capture_output=True,
            text=True,
            cwd=cwd,
            env=_build_env(),
            timeout=timeout,
        )

        if result.returncode != 0 or not result.stdout.strip():
            raise RuntimeError(
                f"pi exited {result.returncode}: {result.stderr[:200]}"
            )

        data = _extract_json_object(result.stdout)
        recommendation = _validate_recommendation(data, vertical)
        log.info(
            "template_recommended",
            business=business_name,
            vertical=vertical,
            template=recommendation["template"],
            rationale=recommendation["rationale"][:120],
        )
        return recommendation

    except Exception as exc:
        log.warning(
            "template_analysis_failed",
            business=business_name,
            vertical=vertical,
            error=str(exc),
        )
        fallback = dict(FALLBACK_RECOMMENDATION)
        fallback["template"] = template_suggestion
        fallback["rationale"] = f"Fallback to vertical default ({template_suggestion}): {str(exc)[:200]}"
        fallback["trust_signals"] = list(TRUST_SIGNAL_DEFAULTS.get(template_suggestion, TRUST_SIGNAL_DEFAULTS["general"]))
        return fallback
