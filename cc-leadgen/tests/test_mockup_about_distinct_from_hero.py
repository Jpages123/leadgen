"""Regression tests: about.jpg (and gallery tiles) must not duplicate hero.jpg.

Background
----------
2026-08-31 user feedback: generated mockups showed the identical photo for
both the hero section and the about section. Root cause was two-fold:

  1. ``copy_assets()``'s "about" fallback explicitly reused the hero path
     whenever the pool-based scorer didn't find a dedicated 4:3 candidate
     for "about" (``app/workers/mockup_helpers/build.py``).
  2. The ``mockup_copy_assets`` Pi tool never actually exposed a
     ``candidate_pool`` parameter, so every real call ran in "legacy" mode
     with an empty pool — the sophisticated pool-based scorer
     (``asset_scorer.py``) never got a chance to pick a distinct 4:3
     candidate for "about" in practice.

This file pins the ``build.py`` fix (#1): in pool mode, "about" (and gallery
slots) must prefer any other unused real photo over duplicating hero, and
fall back to a Pillow gradient/solid-colour placeholder — NOT a
hero-duplicate — only when the pool genuinely has nothing else. Legacy
no-pool-mode behaviour (about = hero) is intentionally preserved and
covered by the existing ``test_copy_assets_falls_back_to_hints_when_pool_empty``
test in ``test_mockup_copy_assets_pool.py``.

Fix #2 (the ``mockup_copy_assets`` Pi tool now exposing ``candidate_pool``)
lives in ``skills/mockup-builder.ts`` and isn't unit-testable from Python —
verified by manual review + a live smoke test instead (see
docs/PHASE_P_PLAN.md addendum).

Run with:
    cd ~/installedApps/leadgen/cc-leadgen && source .venv/bin/activate \\
        && pytest -c /dev/null tests/test_mockup_about_distinct_from_hero.py -v \\
            --no-header -p no:cacheprovider
"""
from __future__ import annotations

import os
import sys
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from PIL import Image

from app.workers.mockup_helpers import build


def make_jpeg(path: Path, width: int, height: int, color=(120, 120, 120)) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (width, height), color).save(path, "JPEG", quality=85)


def _fake_assignments(hero_path: str, **overrides) -> dict:
    base = {
        "logo": None, "hero": hero_path, "about": None,
        "gallery/1": None, "gallery/2": None, "gallery/3": None, "gallery/4": None,
    }
    base.update(overrides)
    return base


def test_about_uses_next_unused_pool_candidate_when_scorer_leaves_it_empty(tmp_path, monkeypatch):
    """If the scorer's own pass leaves 'about' empty despite an unused real
    photo existing in the pool, copy_assets must pick that unused photo
    over duplicating hero. Scorer output is mocked for determinism — this
    isolates the copy_assets fallback logic from asset_scorer's thresholds."""
    fake_root = tmp_path / "builds"
    fake_root.mkdir()
    monkeypatch.setattr(
        "app.workers.mockup_helpers.build.mockup_project_dir",
        lambda slug: fake_root / slug,
    )

    src_dir = tmp_path / "src"
    hero_src = src_dir / "hero.jpg"
    extra_src = src_dir / "extra.jpg"
    make_jpeg(hero_src, 1600, 900, color=(200, 50, 50))
    make_jpeg(extra_src, 1200, 900, color=(50, 200, 50))

    with patch(
        "app.workers.mockup_helpers.build.assign_slots",
        return_value=_fake_assignments(str(hero_src)),
    ):
        build.copy_assets(
            slug="scorer-empty-about-test",
            hero_path=str(hero_src),
            candidate_pool=[str(hero_src), str(extra_src)],
            business_name="Scorer Co",
            accent_color="#1a4d5c",
        )

    images_dir = fake_root / "scorer-empty-about-test" / "scorer-empty-about-test" / "public" / "images"
    hero_bytes = (images_dir / "hero.jpg").read_bytes()
    about_bytes = (images_dir / "about.jpg").read_bytes()
    assert hero_bytes != about_bytes, "about.jpg duplicated hero.jpg despite an unused candidate being available"


def test_about_falls_back_to_gradient_not_hero_when_pool_has_only_one_photo(tmp_path, monkeypatch):
    """Pool mode with exactly 1 real photo total: about must be a gradient,
    not a hero-duplicate — there's genuinely nothing else to use."""
    fake_root = tmp_path / "builds"
    fake_root.mkdir()
    monkeypatch.setattr(
        "app.workers.mockup_helpers.build.mockup_project_dir",
        lambda slug: fake_root / slug,
    )

    src_dir = tmp_path / "src"
    hero_src = src_dir / "only-photo.jpg"
    make_jpeg(hero_src, 1600, 900, color=(200, 50, 50))

    with patch(
        "app.workers.mockup_helpers.build.assign_slots",
        return_value=_fake_assignments(str(hero_src)),
    ):
        result = build.copy_assets(
            slug="single-photo-test",
            hero_path=str(hero_src),
            candidate_pool=[str(hero_src)],
            business_name="Single Photo Co",
            accent_color="#1a4d5c",
        )

    images_dir = fake_root / "single-photo-test" / "single-photo-test" / "public" / "images"
    hero_bytes = (images_dir / "hero.jpg").read_bytes()
    about_bytes = (images_dir / "about.jpg").read_bytes()
    assert hero_bytes != about_bytes, "about.jpg duplicated the only real photo instead of using a gradient"
    assert result["image_audit"]["logo_source"] != "missing"


def test_legacy_no_pool_mode_still_reuses_hero_for_about(tmp_path, monkeypatch):
    """Legacy behaviour (no candidate_pool at all) is unchanged — pinned
    separately in test_mockup_copy_assets_pool.py, re-asserted here for
    this fix's context: with an empty pool, we can't offer any better
    alternative than duplicating hero, so we intentionally still do."""
    fake_root = tmp_path / "builds"
    fake_root.mkdir()
    monkeypatch.setattr(
        "app.workers.mockup_helpers.build.mockup_project_dir",
        lambda slug: fake_root / slug,
    )

    src_dir = tmp_path / "src"
    hero_src = src_dir / "the-hero.jpg"
    make_jpeg(hero_src, 1600, 900)

    build.copy_assets(
        slug="legacy-mode-test",
        hero_path=str(hero_src),
        candidate_pool=None,
        business_name="Legacy Co",
        accent_color="#1a4d5c",
    )
    images_dir = fake_root / "legacy-mode-test" / "legacy-mode-test" / "public" / "images"
    hero_bytes = (images_dir / "hero.jpg").read_bytes()
    about_bytes = (images_dir / "about.jpg").read_bytes()
    assert hero_bytes == about_bytes, "legacy no-pool mode should still reuse hero for about"


def test_gallery_prefers_distinct_photo_over_hero_duplicate(tmp_path, monkeypatch):
    """A gallery slot the scorer left empty should also prefer an unused
    real photo over duplicating hero, when one is available in the pool."""
    fake_root = tmp_path / "builds"
    fake_root.mkdir()
    monkeypatch.setattr(
        "app.workers.mockup_helpers.build.mockup_project_dir",
        lambda slug: fake_root / slug,
    )

    src_dir = tmp_path / "src"
    hero_src = src_dir / "hero.jpg"
    about_src = src_dir / "about.jpg"
    extra_src = src_dir / "extra.jpg"
    make_jpeg(hero_src, 1600, 900, color=(200, 50, 50))
    make_jpeg(about_src, 1200, 900, color=(50, 200, 50))
    make_jpeg(extra_src, 1000, 1000, color=(50, 50, 200))

    with patch(
        "app.workers.mockup_helpers.build.assign_slots",
        return_value=_fake_assignments(str(hero_src), about=str(about_src)),
    ):
        build.copy_assets(
            slug="gallery-distinct-test",
            hero_path=str(hero_src),
            candidate_pool=[str(hero_src), str(about_src), str(extra_src)],
            business_name="Gallery Co",
            accent_color="#1a4d5c",
        )

    images_dir = fake_root / "gallery-distinct-test" / "gallery-distinct-test" / "public" / "images"
    hero_bytes = (images_dir / "hero.jpg").read_bytes()
    gallery1_bytes = (images_dir / "gallery" / "1.jpg").read_bytes()
    assert hero_bytes != gallery1_bytes, "gallery/1.jpg duplicated hero.jpg despite a second candidate being available"
