"""Regression tests for the mockup image fallback chain.

Background
----------
Session 9 (2026-07-09) found that the mockup build silently deployed
zero-byte placeholder images whenever the scraped audit assets were
missing — which happened for every regenerated mockup because assets
were stored in ``/tmp/cc_audits/`` and disappeared between audit and
build. The fix introduces:

  1. Durable storage at ``<project>/.cache/audits/<slug>-*.{jpg,png}``
  2. Pillow-based fallback chain for every image slot
  3. Fail-fast validation that the build refuses to deploy empty images

These tests pin those behaviours.

Run with:
    cd ~/installedApps/leadgen/cc-leadgen && source .venv/bin/activate \\
        && pytest -c /dev/null tests/test_mockup_image_fallbacks.py -v \\
            --no-header -p no:cacheprovider
"""
from __future__ import annotations

import os
import sys
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from PIL import Image

from app.utils import audit_assets, placeholder_image


# ─── audit_assets.resolve() ──────────────────────────────────────────────────

def test_resolve_returns_none_for_missing_slug():
    """A slug that's never been audited returns None — not an exception."""
    assert audit_assets.resolve("never-audited-business-xyz", "logo") is None


def test_resolve_finds_file_in_new_location(tmp_path):
    """resolve() finds a file at the durable .cache/audits/ location."""
    slug = "test-resolve-new"
    with patch.object(audit_assets, "audit_assets_dir", return_value=tmp_path):
        target = audit_assets.asset_path(slug, "logo")
        target.write_bytes(b"fake-jpg-bytes")
        found = audit_assets.resolve(slug, "logo")
        assert found == target
        assert found.exists()
        assert found.stat().st_size > 0


def test_resolve_falls_back_to_legacy_tmp(tmp_path):
    """If only /tmp/cc_audits/<slug>-logo.jpg exists, still find it."""
    slug = "test-resolve-legacy"
    legacy_dir = tmp_path / "legacy"
    legacy_file = legacy_dir / f"{slug}-logo.jpg"
    legacy_file.parent.mkdir(parents=True, exist_ok=True)
    legacy_file.write_bytes(b"legacy-jpg-bytes")

    with patch.object(audit_assets, "_LEGACY_AUDIT_DIR", legacy_dir), \
         patch.object(audit_assets, "audit_assets_dir", return_value=tmp_path / "cache"):
        found = audit_assets.resolve(slug, "logo")
        assert found == legacy_file


def test_resolve_returns_none_for_zero_byte_file(tmp_path):
    """A zero-byte file in either location is treated as missing."""
    slug = "test-resolve-empty"
    with patch.object(audit_assets, "audit_assets_dir", return_value=tmp_path):
        target = audit_assets.asset_path(slug, "logo")
        target.write_bytes(b"")
        assert audit_assets.resolve(slug, "logo") is None


def test_asset_path_uses_flat_layout():
    """asset_path returns <base>/<slug>-<kind>.<ext> (flat, matches legacy scraper)."""
    with patch.object(audit_assets, "audit_assets_dir", return_value=Path("/fake/cache")):
        p = audit_assets.asset_path("my-slug", "logo")
        assert p == Path("/fake/cache/my-slug-logo.jpg")
        p = audit_assets.asset_path("my-slug", "gallery-1")
        assert p == Path("/fake/cache/my-slug-gallery-1.jpg")


# ─── placeholder_image generators ────────────────────────────────────────────

def test_text_logo_produces_valid_jpeg(tmp_path):
    """text_logo writes a non-empty JPEG and the file is decodable."""
    dest = tmp_path / "logo.jpg"
    size = placeholder_image.text_logo(
        "DATRA CONSTRUCTION",
        accent_hex="#1a4d5c",
        dest=dest,
    )
    assert size > 1000, f"Logo is suspiciously small: {size} bytes"
    assert dest.exists()
    with Image.open(dest) as img:
        assert img.format == "JPEG"
        assert img.size == (480, 120)


def test_text_logo_handles_unknown_color(tmp_path):
    """Invalid hex falls back to a slate colour, not a crash."""
    dest = tmp_path / "logo.jpg"
    size = placeholder_image.text_logo("X", accent_hex="garbage", dest=dest)
    assert size > 1000


def test_text_logo_handles_empty_name(tmp_path):
    """Empty business name doesn't crash — uses 'Business' as fallback."""
    dest = tmp_path / "logo.jpg"
    size = placeholder_image.text_logo("", accent_hex="#000000", dest=dest)
    assert size > 1000


def test_text_logo_shrinks_long_names(tmp_path):
    """A very long business name shrinks the font to fit width."""
    dest = tmp_path / "logo.jpg"
    long_name = "VERY LONG BUSINESS NAME THAT EXCEEDS WIDTH VERY LONG BUSINESS NAME"
    size = placeholder_image.text_logo(long_name, "#1a4d5c", dest, width=400)
    assert size > 1000


def test_solid_image_produces_valid_jpeg(tmp_path):
    dest = tmp_path / "hero.jpg"
    size = placeholder_image.solid_image("#1a4d5c", dest, width=800, height=400)
    assert size > 1000
    with Image.open(dest) as img:
        assert img.format == "JPEG"
        assert img.size == (800, 400)


def test_gradient_image_produces_valid_jpeg(tmp_path):
    dest = tmp_path / "about.jpg"
    size = placeholder_image.gradient_image("#1a4d5c", dest, width=800, height=400, label="About Us")
    assert size > 1000
    with Image.open(dest) as img:
        assert img.format == "JPEG"


# ─── min-size invariant — the bug we're guarding against ─────────────────────

def test_text_logo_never_produces_zero_byte_output(tmp_path):
    """Regression: pre-fix Pexels fallback wrote 0-byte files. Pin it."""
    for accent in ("#1a4d5c", "#000000", "#ffffff", "#e85d04", "garbage"):
        dest = tmp_path / f"logo-{accent.replace('#','')}.jpg"
        size = placeholder_image.text_logo("Test", accent, dest)
        assert size > 1000, f"Placeholder produced {size} bytes for accent={accent}"


# ─── mockup_generator build validation ───────────────────────────────────────

def test_validate_build_images_rejects_missing(tmp_path):
    """If a critical image is missing, _validate_build_images raises."""
    from app.workers.mockup_generator import _validate_build_images

    import pytest
    with pytest.raises(RuntimeError, match="missing"):
        _validate_build_images(tmp_path)


def test_validate_build_images_rejects_zero_byte(tmp_path):
    """If a critical image is 0 bytes, validation raises (the pre-fix bug)."""
    from app.workers.mockup_generator import _validate_build_images

    (tmp_path / "logo.jpg").write_bytes(b"")
    (tmp_path / "hero.jpg").write_bytes(b"\xff\xd8\xff\xe0" + b"\x00" * 100)
    (tmp_path / "about.jpg").write_bytes(b"\xff\xd8\xff\xe0" + b"\x00" * 100)
    (tmp_path / "gallery").mkdir()
    for i in range(1, 5):
        (tmp_path / "gallery" / f"{i}.jpg").write_bytes(b"\xff\xd8\xff\xe0" + b"\x00" * 100)

    import pytest
    with pytest.raises(RuntimeError, match="zero bytes"):
        _validate_build_images(tmp_path)


def test_validate_build_images_passes_when_complete(tmp_path):
    """Validation passes when all critical images are > 1KB."""
    from app.workers.mockup_generator import _validate_build_images

    (tmp_path / "logo.jpg").write_bytes(b"\xff\xd8\xff\xe0" + b"\x00" * 2000)
    (tmp_path / "hero.jpg").write_bytes(b"\xff\xd8\xff\xe0" + b"\x00" * 5000)
    (tmp_path / "about.jpg").write_bytes(b"\xff\xd8\xff\xe0" + b"\x00" * 3000)
    (tmp_path / "gallery").mkdir()
    for i in range(1, 5):
        (tmp_path / "gallery" / f"{i}.jpg").write_bytes(b"\xff\xd8\xff\xe0" + b"\x00" * 2000)

    # Should not raise
    _validate_build_images(tmp_path)