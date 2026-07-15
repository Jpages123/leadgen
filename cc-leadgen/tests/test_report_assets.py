"""Regression tests for ``app.utils.report_assets``.

Background
----------
Session 15 (2026-07-15) introduced durable PDF storage at
``<project>/.cache/reports/`` after the Limelight test email exposed that
``/tmp/cc_reports/<slug>.pdf`` could be silently dropped between audit
and send. The resolver and regen functions are the safety net; these
tests pin their behaviour.

Tests run hermetically — they monkeypatch ``report_assets.report_dir``
to point at a tmp_path so they never touch the real ``.cache/`` dir
(which on a live worker is owned by root and would block test writes).

Run with::

    cd ~/installedApps/leadgen/cc-leadgen && source .venv/bin/activate \\
        && pytest -c /dev/null tests/test_report_assets.py -v \\
            --no-header -p no:cacheprovider
"""
from __future__ import annotations

import os
import sys
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest

from app.utils import report_assets


# ─── Fixture: redirect report_dir to tmp_path ─────────────────────────────────

@pytest.fixture
def fake_cache(tmp_path, monkeypatch):
    """Replace ``report_assets.report_dir()`` with a tmp_path-based fake.

    Also patches ``_LEGACY_REPORT_DIR`` to a sibling tmp dir so legacy
    fallback tests can write there too.
    """
    fake_durable = tmp_path / "reports"
    fake_durable.mkdir()
    fake_legacy = tmp_path / "legacy_reports"
    fake_legacy.mkdir()
    monkeypatch.setattr(report_assets, "report_dir", lambda: fake_durable)
    monkeypatch.setattr(report_assets, "_LEGACY_REPORT_DIR", fake_legacy)
    return {"durable": fake_durable, "legacy": fake_legacy}


# ─── report_path() / report_dir() ─────────────────────────────────────────────

def test_report_path_uses_business_name_slug(fake_cache):
    """Slug-based path matches the legacy convention."""
    p = report_assets.report_path("limelight-event-hire")
    assert p.name == "limelight-event-hire.pdf"
    assert p.parent == fake_cache["durable"]


def test_report_path_rejects_empty_slug(fake_cache):
    """Empty / whitespace slug raises ValueError (no silent zero-byte PDF)."""
    with pytest.raises(ValueError):
        report_assets.report_path("")
    with pytest.raises(ValueError):
        report_assets.report_path("   ")


# ─── resolve() ───────────────────────────────────────────────────────────────

def test_resolve_returns_none_for_missing_slug(fake_cache):
    """A slug that's never been audited returns None."""
    assert report_assets.resolve("nonexistent-business-zzz-xyz") is None


def test_resolve_finds_durable_first(fake_cache):
    """If a PDF exists in the durable location, ``resolve()`` returns it
    even when a legacy ``/tmp`` copy also exists."""
    durable = fake_cache["durable"] / "test-resolve-durable.pdf"
    durable.write_bytes(b"%PDF-1.4 fake content")
    legacy = fake_cache["legacy"] / "test-resolve-durable.pdf"
    legacy.write_bytes(b"%PDF-1.4 legacy")
    result = report_assets.resolve("test-resolve-durable")
    assert result == durable


def test_resolve_falls_back_to_legacy(fake_cache):
    """If only a legacy ``/tmp`` copy exists, ``resolve()`` returns it."""
    legacy = fake_cache["legacy"] / "test-legacy-only.pdf"
    legacy.write_bytes(b"%PDF-1.4 legacy only")
    result = report_assets.resolve("test-legacy-only")
    assert result == legacy


def test_resolve_zero_byte_file_treated_as_missing(fake_cache):
    """A zero-byte PDF at the durable location is treated as missing —
    callers should regenerate rather than attach an empty file."""
    durable = fake_cache["durable"] / "test-resolve-empty.pdf"
    durable.write_bytes(b"")
    legacy = fake_cache["legacy"] / "test-resolve-empty.pdf"
    legacy.write_bytes(b"")
    result = report_assets.resolve("test-resolve-empty")
    assert result is None


# ─── slug helper ─────────────────────────────────────────────────────────────

def test_slug_from_name_matches_legacy():
    """``_slug_from_name`` mirrors the helper in web_audit_report.py."""
    assert report_assets._slug_from_name("Limelight Event Hire") == "limelight-event-hire"
    assert report_assets._slug_from_name("D & G Plumbing!") == "d-g-plumbing"
    assert report_assets._slug_from_name("---") == "lead"  # empty fallback
    long = "a" * 100
    assert len(report_assets._slug_from_name(long)) == 60


# ─── regenerate_pdf_for_lead() ───────────────────────────────────────────────

def test_regenerate_skips_when_lead_missing_business_name(fake_cache):
    """Lead without business_name returns None and logs."""
    class FakeLead:
        website = "https://example.com"
        id = "abc"
    assert report_assets.regenerate_pdf_for_lead(FakeLead()) is None


def test_regenerate_skips_when_lead_missing_website(fake_cache):
    """Lead without website returns None and logs."""
    class FakeLead:
        business_name = "Acme Plumbing"
        id = "abc"
    assert report_assets.regenerate_pdf_for_lead(FakeLead()) is None


def test_regenerate_returns_existing_durable_pdf_without_regen(fake_cache):
    """If a durable PDF already exists, ``regenerate_pdf_for_lead`` returns
    it directly without invoking the PDF generator (cheap fast-path)."""
    class FakeLead:
        business_name = "test-fastpath"
        website = "https://fastpath.example.com"
        id = "abc"
    durable = fake_cache["durable"] / "test-fastpath.pdf"
    durable.write_bytes(b"%PDF-1.4 existing")
    with patch("app.reports.web_audit_report.generate_pdf") as mock_gen:
        result = report_assets.regenerate_pdf_for_lead(FakeLead())
    assert result == str(durable)
    assert mock_gen.call_count == 0


def test_regenerate_returns_none_when_generate_pdf_fails(fake_cache):
    """If ``generate_pdf`` raises, ``regenerate_pdf_for_lead`` catches and
    returns None (doesn't propagate the exception to the email sender)."""
    class FakeLead:
        business_name = "test-fail"
        website = "https://fail.example.com"
        id = "abc"
    with patch("app.reports.web_audit_report.generate_pdf",
               side_effect=RuntimeError("weasyprint blew up")):
        result = report_assets.regenerate_pdf_for_lead(FakeLead())
    assert result is None
    # No file was written
    assert not (fake_cache["durable"] / "test-fail.pdf").exists()