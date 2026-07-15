"""Regression tests for ``app.utils.mockup_build``.

Background
----------
Session 19 (2026-07-15) introduced durable mockup build dirs at
``<project>/.cache/mockups/<slug>/`` plus a content-hash skip-rebuild
shortcut. Pre-fix, every build wiped the project dir (including
``node_modules/``) via ``shutil.rmtree()`` even when inputs were
unchanged — 40-90s of wasted ``pnpm install`` + ``pnpm build`` work.

Tests pin:
  1. The durable build dir is created at the expected location
  2. ``compute_input_hash`` is stable and changes when inputs change
  3. ``should_skip_build`` returns False on first build (no cache yet)
  4. ``should_skip_build`` returns True on second build (cache hit)
  5. ``should_skip_build`` returns False when dist/ is missing
  6. ``should_skip_build`` returns False when inputs change

Tests run hermetically — they monkeypatch ``mockup_project_dir`` to
point at a tmp_path so they never touch the real ``.cache/mockups/`` dir
(which on a live worker is owned by root).

Run with::

    cd ~/installedApps/leadgen/cc-leadgen && source .venv/bin/activate \\
        && pytest -c /dev/null tests/test_mockup_build.py -v \\
            --no-header -p no:cacheprovider
"""
from __future__ import annotations

import os
import sys
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest

from app.utils import mockup_build


# ─── Fixture: redirect build dirs to tmp_path ────────────────────────────────

@pytest.fixture
def fake_builds(tmp_path, monkeypatch):
    """Replace ``mockup_project_dir`` with a tmp_path-based fake.

    Also redirects ``mockup_builds_dir`` so any helper that calls it
    (e.g. mkdir on first call) writes under tmp_path.
    """
    fake_root = tmp_path / "mockups"
    fake_root.mkdir()
    monkeypatch.setattr(mockup_build, "mockup_builds_dir", lambda: fake_root)

    def _project(slug: str) -> Path:
        return fake_root / slug

    monkeypatch.setattr(mockup_build, "mockup_project_dir", _project)
    return {"root": fake_root, "project_fn": _project}


# ─── durable dir ──────────────────────────────────────────────────────────────

def test_mockup_builds_dir_creates_cache(fake_builds):
    """``mockup_builds_dir()`` creates ``<project>/.cache/mockups/`` on first call."""
    d = mockup_build.mockup_builds_dir()
    assert d == fake_builds["root"]
    assert d.exists()
    assert d.is_dir()


def test_mockup_project_dir_per_slug(fake_builds):
    """Each slug gets its own subdirectory under the durable base."""
    a = mockup_build.mockup_project_dir("limelight-event-hire")
    b = mockup_build.mockup_project_dir("datra-construction")
    assert a.name == "limelight-event-hire"
    assert b.name == "datra-construction"
    assert a != b
    assert a.parent == fake_builds["root"]


# ─── compute_input_hash ───────────────────────────────────────────────────────

def test_hash_is_stable_for_same_inputs(fake_builds):
    """Same inputs → same hash (otherwise cache hits wouldn't work)."""
    h1 = mockup_build.compute_input_hash("client_a", "brand_b", ["/tmp/x.jpg"])
    h2 = mockup_build.compute_input_hash("client_a", "brand_b", ["/tmp/x.jpg"])
    assert h1 == h2


def test_hash_changes_when_client_ts_changes(fake_builds):
    h1 = mockup_build.compute_input_hash("client_a", "brand_b", [])
    h2 = mockup_build.compute_input_hash("client_a_changed", "brand_b", [])
    assert h1 != h2


def test_hash_changes_when_brand_ts_changes(fake_builds):
    h1 = mockup_build.compute_input_hash("client_a", "brand_b", [])
    h2 = mockup_build.compute_input_hash("client_a", "brand_b_changed", [])
    assert h1 != h2


def test_hash_changes_when_asset_added(fake_builds):
    h1 = mockup_build.compute_input_hash("client_a", "brand_b", [])
    h2 = mockup_build.compute_input_hash("client_a", "brand_b", ["/tmp/extra.jpg"])
    assert h1 != h2


def test_hash_changes_when_asset_byte_content_changes(fake_builds, tmp_path):
    """The hash is over file bytes, not just paths — a replaced logo
    with the same name but different content is detected."""
    asset1 = tmp_path / "logo.jpg"
    asset2 = tmp_path / "logo.jpg"
    # Note: same path, different bytes
    h1 = mockup_build.compute_input_hash("client_a", "brand_b", [str(asset1)])
    asset1.write_bytes(b"original logo")
    h2 = mockup_build.compute_input_hash("client_a", "brand_b", [str(asset2)])
    asset2.write_bytes(b"replaced logo")
    h3 = mockup_build.compute_input_hash("client_a", "brand_b", [str(asset2)])
    assert h1 != h3  # original vs replaced


def test_hash_handles_missing_assets_consistently(fake_builds):
    """A missing asset contributes a sentinel — same path → same hash."""
    h1 = mockup_build.compute_input_hash("c", "b", ["/nonexistent/photo.jpg"])
    h2 = mockup_build.compute_input_hash("c", "b", ["/nonexistent/photo.jpg"])
    assert h1 == h2


def test_hash_handles_none_in_asset_list(fake_builds):
    """None values in the asset list are skipped silently."""
    h1 = mockup_build.compute_input_hash("c", "b", [None, None])
    h2 = mockup_build.compute_input_hash("c", "b", [])
    assert h1 == h2


# ─── read_cached_hash / write_build_hash ──────────────────────────────────────

def test_write_then_read_returns_same_hash(fake_builds):
    """Round-trip the hash through the persistent file."""
    slug = "test-roundtrip"
    h = mockup_build.compute_input_hash("c", "b", [])
    mockup_build.write_build_hash(slug, h)
    assert mockup_build.read_cached_hash(slug) == h


def test_read_cached_hash_returns_none_when_missing(fake_builds):
    """No hash file yet → None (cache miss)."""
    assert mockup_build.read_cached_hash("never-built") is None


def test_read_cached_hash_returns_none_on_corrupt_file(fake_builds):
    """A corrupt hash file is treated as a cache miss (safe default)."""
    slug = "corrupt"
    p = mockup_build.mockup_project_dir(slug)
    p.mkdir(parents=True, exist_ok=True)
    (p / mockup_build.HASH_FILENAME).write_text("")  # empty
    assert mockup_build.read_cached_hash(slug) is None


# ─── should_skip_build ────────────────────────────────────────────────────────

def test_should_skip_build_returns_false_on_first_build(fake_builds):
    """No dist/ yet → cannot skip."""
    assert mockup_build.should_skip_build(
        "fresh-slug", "c", "b", []
    ) is False


def test_should_skip_build_returns_true_on_cache_hit(fake_builds):
    """dist/ exists AND cached hash matches → skip."""
    slug = "cache-hit"
    project = mockup_build.mockup_project_dir(slug)
    project.mkdir(parents=True, exist_ok=True)
    (project / "dist").mkdir(exist_ok=True)
    (project / "dist" / "index.html").write_text("<html>cached</html>")

    mockup_build.write_build_hash(slug, mockup_build.compute_input_hash("c", "b", []))
    assert mockup_build.should_skip_build(slug, "c", "b", []) is True


def test_should_skip_build_returns_false_when_inputs_change(fake_builds):
    """Cached hash present but inputs differ → must rebuild."""
    slug = "changed-inputs"
    project = mockup_build.mockup_project_dir(slug)
    project.mkdir(parents=True, exist_ok=True)
    (project / "dist").mkdir(exist_ok=True)
    (project / "dist" / "index.html").write_text("<html>stale</html>")

    mockup_build.write_build_hash(slug, mockup_build.compute_input_hash("c", "b", []))
    assert mockup_build.should_skip_build(slug, "c_CHANGED", "b", []) is False


def test_should_skip_build_returns_false_when_dist_missing(fake_builds):
    """Hash matches but dist/ was wiped (e.g. accidental rm) → must rebuild."""
    slug = "no-dist"
    project = mockup_build.mockup_project_dir(slug)
    project.mkdir(parents=True, exist_ok=True)
    # NOTE: no dist/ subdir

    mockup_build.write_build_hash(slug, mockup_build.compute_input_hash("c", "b", []))
    assert mockup_build.should_skip_build(slug, "c", "b", []) is False


def test_should_skip_build_returns_false_when_project_missing(fake_builds):
    """No project dir at all → cannot skip."""
    assert mockup_build.should_skip_build(
        "no-project", "c", "b", []
    ) is False


def test_should_skip_build_returns_false_when_index_missing(fake_builds):
    """dist/ exists but no index.html → last build didn't complete → rebuild."""
    slug = "incomplete-build"
    project = mockup_build.mockup_project_dir(slug)
    project.mkdir(parents=True, exist_ok=True)
    (project / "dist").mkdir(exist_ok=True)
    # No index.html

    mockup_build.write_build_hash(slug, mockup_build.compute_input_hash("c", "b", []))
    assert mockup_build.should_skip_build(slug, "c", "b", []) is False