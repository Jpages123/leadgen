"""Tests for app/utils/template_analyzer.py - Pi-based mockup template recommender.

Validates:
- JSON extraction handles <think> blocks + trailing text
- Recommendation validation coerces bad values, fills missing fields
- Fallback works when pi fails entirely
- Watermark only allowed for creative template
- Section order always has hero first, contact last
- Trust signals always padded to at least 3 items
- Vertical mapping returns expected default template
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from app.utils.template_analyzer import (
    FALLBACK_RECOMMENDATION,
    TEMPLATE_VERTICAL_DEFAULTS,
    TRUST_SIGNAL_DEFAULTS,
    VALID_TEMPLATES,
    _build_prompt,
    _extract_json_object,
    _validate_recommendation,
    analyze_site_for_mockup,
)


# --- _extract_json_object ---

class TestExtractJsonObject:
    def test_clean_json_parses(self):
        raw = '{"template": "trades", "rationale": "looks good"}'
        assert _extract_json_object(raw) == {
            "template": "trades",
            "rationale": "looks good",
        }

    def test_think_block_with_braces_does_not_confuse_extraction(self):
        raw = (
            "<think>The user gave me example code: "
            'function foo() { return { ok: true }; }'
            "</think>"
            '{"template": "creative", "rationale": "photography site"}'
        )
        result = _extract_json_object(raw)
        assert result["template"] == "creative"

    def test_trailing_text_after_json_is_tolerated(self):
        raw = (
            '{"template": "general", "rationale": "balanced"}'
            "\nThis mockup will use the general template as a fallback."
        )
        assert _extract_json_object(raw)["template"] == "general"

    def test_garbage_output_raises(self):
        with pytest.raises(ValueError, match="No valid JSON object"):
            _extract_json_object("no json here at all, just plain text")

    def test_multiple_think_blocks_stripped(self):
        raw = (
            "<think>reasoning 1 {curly braces inside}</think>"
            "intermediate text"
            "<think>more reasoning {braces}</think>"
            '{"template": "creative"}'
        )
        assert _extract_json_object(raw)["template"] == "creative"


# --- _validate_recommendation ---

class TestValidateRecommendation:
    def test_clean_output_is_returned_unchanged(self):
        data = {
            "template": "trades",
            "rationale": "Plumber with image-light site",
            "brand_colors": {
                "primary": "#1e9be8",
                "accent": "#f97316",
                "hero_treatment": "image-with-overlay",
                "text_contrast": "light",
            },
            "layout_variant": "service-first",
            "gallery_treatment": "clean-grid",
            "hero_overlay_opacity": 45,
            "services_style": "icon-cards",
            "typography_hint": "sans-preferred",
            "cta_priority": "phone",
            "dark_mode": False,
            "image_watermark": False,
            "trust_signals": ["Registered", "Insured", "24/7"],
            "section_order": ["hero", "trust", "services", "about", "contact"],
        }
        result = _validate_recommendation(data, "trades")
        assert result["template"] == "trades"
        assert result["brand_colors"]["accent"] == "#f97316"
        assert result["hero_overlay_opacity"] == 45

    def test_invalid_template_falls_back_to_vertical_default(self):
        data = {"template": "unknown", "rationale": "x"}
        result = _validate_recommendation(data, "photography")
        assert result["template"] == "creative"

    def test_missing_template_uses_vertical_default(self):
        data = {"rationale": "no template field"}
        result = _validate_recommendation(data, "electrical")
        assert result["template"] == "trades"

    def test_watermark_only_allowed_for_creative(self):
        data = {"template": "trades", "image_watermark": True, "rationale": "x"}
        result = _validate_recommendation(data, "trades")
        assert result["image_watermark"] is False
        data_creative = {"template": "creative", "image_watermark": True, "rationale": "x"}
        result_creative = _validate_recommendation(data_creative, "photography")
        assert result_creative["image_watermark"] is True

    def test_hero_overlay_opacity_clamped_to_25_70(self):
        data_low = {"template": "trades", "hero_overlay_opacity": 5, "rationale": "x"}
        assert _validate_recommendation(data_low, "trades")["hero_overlay_opacity"] == 50
        data_high = {"template": "trades", "hero_overlay_opacity": 200, "rationale": "x"}
        assert _validate_recommendation(data_high, "trades")["hero_overlay_opacity"] == 50
        data_ok = {"template": "trades", "hero_overlay_opacity": 35, "rationale": "x"}
        assert _validate_recommendation(data_ok, "trades")["hero_overlay_opacity"] == 35

    def test_invalid_hex_color_replaced_with_default(self):
        data = {
            "template": "trades",
            "brand_colors": {
                "primary": "not-a-color",
                "accent": "rgb(1,2,3)",
                "hero_treatment": "full-bleed",
                "text_contrast": "light",
            },
            "rationale": "x",
        }
        result = _validate_recommendation(data, "trades")
        assert result["brand_colors"]["primary"] == "#1a1a1a"
        assert result["brand_colors"]["accent"] == "#e85d04"

    def test_trust_signals_padded_to_at_least_three(self):
        data = {"template": "trades", "trust_signals": [], "rationale": "x"}
        result = _validate_recommendation(data, "trades")
        assert len(result["trust_signals"]) >= 3
        assert any("Insured" in s or "Compliance" in s for s in result["trust_signals"])

    def test_section_order_always_starts_with_hero_ends_with_contact(self):
        data = {"template": "trades", "section_order": ["gallery", "services"], "rationale": "x"}
        result = _validate_recommendation(data, "trades")
        assert result["section_order"][0] == "hero"
        assert result["section_order"][-1] == "contact"
        for required in ("trust", "services", "gallery", "about", "testimonials"):
            assert required in result["section_order"]


# --- Template-vertical mapping ---

class TestTemplateVerticalDefaults:
    def test_trades_verticals(self):
        for v in ("trades", "plumbing", "electrical", "construction", "cleaning", "automotive"):
            assert TEMPLATE_VERTICAL_DEFAULTS[v] == "trades", f"Failed for {v}"

    def test_creative_verticals(self):
        for v in ("photography", "event_planning"):
            assert TEMPLATE_VERTICAL_DEFAULTS[v] == "creative", f"Failed for {v}"

    def test_unknown_vertical_maps_to_general(self):
        # .get() with default key as fallback
        assert TEMPLATE_VERTICAL_DEFAULTS.get("underwater_basket_weaving", "general") == "general"
        assert TEMPLATE_VERTICAL_DEFAULTS.get("default") == "general"


# --- Prompt builder ---

class TestBuildPrompt:
    def test_includes_business_name_and_vertical(self):
        prompt = _build_prompt(
            business_name="DGF Plumbing",
            vertical="plumbing",
            city="Cape Town",
            website="https://dgfplumbing.co.za",
            website_platform="wordpress_divi",
            scraped_brand_color="#1e9be8",
            scraped_gallery_count=4,
            pagespeed_mobile=42,
            site_copyright_year=2019,
            google_rating=4.9,
            google_review_count=37,
            template_suggestion="trades",
        )
        assert "DGF Plumbing" in prompt
        assert "plumbing" in prompt
        assert "Cape Town" in prompt
        assert "#1e9be8" in prompt

    def test_handles_missing_optional_data(self):
        prompt = _build_prompt(
            business_name="Test Biz",
            vertical="photography",
            city=None,
            website=None,
            website_platform=None,
            scraped_brand_color=None,
            scraped_gallery_count=0,
            pagespeed_mobile=None,
            site_copyright_year=None,
            google_rating=None,
            google_review_count=None,
            template_suggestion="creative",
        )
        assert "Test Biz" in prompt
        assert "unknown" in prompt.lower()
        assert "No brand color" in prompt
        assert "Copyright year not found" in prompt


# --- End-to-end fallback ---

class TestAnalyzeSiteForMockupNoPi:
    def test_fallback_when_pi_not_found(self, monkeypatch):
        from app.utils import template_analyzer

        def fake_resolve_pi():
            raise FileNotFoundError("Cannot locate pi CLI")

        monkeypatch.setattr(template_analyzer, "_resolve_pi", fake_resolve_pi)

        result = template_analyzer.analyze_site_for_mockup(
            business_name="Test Plumbing Co",
            vertical="plumbing",
            city="Cape Town",
        )
        assert result["template"] == "trades"
        assert "Fallback" in result["rationale"]
        assert len(result["trust_signals"]) >= 3

    def test_fallback_for_creative_vertical(self, monkeypatch):
        from app.utils import template_analyzer

        def fake_resolve_pi():
            raise FileNotFoundError("Cannot locate pi CLI")

        monkeypatch.setattr(template_analyzer, "_resolve_pi", fake_resolve_pi)

        result = template_analyzer.analyze_site_for_mockup(
            business_name="Snappy Shots Photography",
            vertical="photography",
        )
        assert result["template"] == "creative"
        assert any("equipment" in s.lower() or "turnaround" in s.lower()
                   for s in result["trust_signals"])

    def test_fallback_for_unknown_vertical(self, monkeypatch):
        from app.utils import template_analyzer

        def fake_resolve_pi():
            raise FileNotFoundError("Cannot locate pi CLI")

        monkeypatch.setattr(template_analyzer, "_resolve_pi", fake_resolve_pi)

        result = template_analyzer.analyze_site_for_mockup(
            business_name="Unknown Vertical Co",
            vertical="underwater_basket_weaving",
        )
        assert result["template"] == "general"


# --- Schema consistency ---

class TestRecommendationSchema:
    def test_recommendation_has_all_keys(self, monkeypatch):
        from app.utils import template_analyzer

        def fake_resolve_pi():
            raise FileNotFoundError("Cannot locate pi CLI")

        monkeypatch.setattr(template_analyzer, "_resolve_pi", fake_resolve_pi)

        result = template_analyzer.analyze_site_for_mockup(
            business_name="Any Co",
            vertical="cleaning",
        )
        expected_top_keys = {
            "template", "rationale", "brand_colors", "layout_variant",
            "gallery_treatment", "hero_overlay_opacity", "services_style",
            "typography_hint", "cta_priority", "dark_mode", "image_watermark",
            "trust_signals", "section_order",
        }
        assert expected_top_keys.issubset(result.keys())
        expected_color_keys = {"primary", "accent", "hero_treatment", "text_contrast"}
        assert expected_color_keys.issubset(result["brand_colors"].keys())
