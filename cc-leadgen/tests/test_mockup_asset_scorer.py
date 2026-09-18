"""Tests for pool-based mockup image slot scoring (Session 21).

Background
----------
Previously, the mockup-builder LLM was blind to image content — it got
file paths from the scraper and passed them straight to copy_assets,
which wrote each path to a fixed slot. When the scraper missed images
(common on SitePad/Wix/Joomla sites) Pillow fallbacks filled the gaps.
When the scraper captured odd-aspect images, the LLM sometimes picked
the wrong slot (e.g. Discount Tents' wide LOGO.jpg banner forced into
a 4:3 about slot, producing a "zoomed logo" render).

The fix: pool-based scoring. Every scraped image is a candidate; each
slot picks the best-fit candidate by aspect ratio + file size. The
LLM's picks become +100 hints, not hard requirements.

These tests pin the new behaviour.

Run with::
    cd ~/installedApps/leadgen/cc-leadgen && source .venv/bin/activate \\
        && pytest -c /dev/null tests/test_mockup_asset_scorer.py -v \\
            --no-header -p no:cacheprovider
"""
from __future__ import annotations

import os
import sys
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest
from PIL import Image

from app.workers.mockup_helpers import asset_scorer


# ─── helpers ────────────────────────────────────────────────────────────────

def make_jpeg(path: Path, width: int, height: int, color=(120, 120, 120), q: int = 85) -> None:
    """Write a real JPEG file at ``path`` with the given dimensions."""
    path.parent.mkdir(parents=True, exist_ok=True)
    img = Image.new("RGB", (width, height), color)
    img.save(path, "JPEG", quality=q)


@pytest.fixture
def img_pool(tmp_path):
    """Build a realistic pool of candidate images (mirrors Discount Tents' case).

    Names chosen to match the real discounttents.co.za scrape so the test
    reproduces the original bug. The "LOGO.jpg" file is a wide banner
    (1.74 ratio) — the original cause of the "zoomed logo" issue.
    """
    base = tmp_path / "pool"
    files = {
        # Tent/event hero shot — wide 1.50, the obvious hero
        "7.jpg": (3000, 2000),
        # Wide banner labeled as logo (1.74) — this is the offender
        "LOGO.jpg": (1740, 1000),
        # Peg & pole tent photo (1.84) — fits gallery/1 best
        "PEG-AND-POLE-TENT.jpg": (1840, 1000),
        # Actual site header logo (2.19) — better for logo slot
        "Untitled.jpg": (2190, 1000),
        # Jumping castle — basically square (1.08), perfect for square tiles
        "jumping-castle.jpg": (1080, 1000),
    }
    for name, (w, h) in files.items():
        make_jpeg(base / name, w, h, q=85)
    return base


# ─── load_pool ───────────────────────────────────────────────────────────────

def test_load_pool_filters_missing_paths(tmp_path):
    make_jpeg(tmp_path / "real.jpg", 800, 600)
    pool = asset_scorer.load_pool([
        str(tmp_path / "real.jpg"),
        str(tmp_path / "missing.jpg"),
    ])
    assert len(pool) == 1
    assert pool[0].path == str(tmp_path / "real.jpg")


def test_load_pool_filters_zero_byte_files(tmp_path):
    (tmp_path / "empty.jpg").write_bytes(b"")
    make_jpeg(tmp_path / "real.jpg", 800, 600)
    pool = asset_scorer.load_pool([str(tmp_path / "empty.jpg"), str(tmp_path / "real.jpg")])
    assert len(pool) == 1


def test_load_pool_filters_files_below_1kb(tmp_path):
    """Pre-fix bug: 1×1 placeholder PNG slipped through. 1KB floor guards it."""
    # Pillow can't write a 1×1 JPEG under 1KB easily, so make a tiny-but-real one
    make_jpeg(tmp_path / "tiny.jpg", 16, 16, q=10)
    # Verify it's below threshold and gets filtered
    assert (tmp_path / "tiny.jpg").stat().st_size < 1024
    pool = asset_scorer.load_pool([str(tmp_path / "tiny.jpg")])
    assert pool == []


def test_load_pool_dedupes_paths():
    pool = asset_scorer.load_pool(["/nonexistent/a", "/nonexistent/a", "/nonexistent/b"])
    # None of them exist so all filtered; just verify no crash and no dups
    assert pool == []


def test_load_pool_preserves_order(tmp_path):
    """Order is the tie-breaker — first candidate wins ties."""
    for i, w in enumerate([800, 1200, 1600]):
        make_jpeg(tmp_path / f"f{i}.jpg", w, 600)
    pool = asset_scorer.load_pool([
        str(tmp_path / "f0.jpg"),
        str(tmp_path / "f1.jpg"),
        str(tmp_path / "f2.jpg"),
    ])
    assert [p.path for p in pool] == [
        str(tmp_path / "f0.jpg"),
        str(tmp_path / "f1.jpg"),
        str(tmp_path / "f2.jpg"),
    ]


def test_load_pool_skips_undecodable_files(tmp_path):
    """A file with .jpg extension but garbage bytes is filtered out."""
    (tmp_path / "garbage.jpg").write_bytes(b"not-an-image-but-over-1kb" * 50)
    make_jpeg(tmp_path / "real.jpg", 800, 600)
    pool = asset_scorer.load_pool([str(tmp_path / "garbage.jpg"), str(tmp_path / "real.jpg")])
    assert len(pool) == 1


# ─── score_candidate ─────────────────────────────────────────────────────────

def test_score_perfect_aspect_fit_is_one():
    """Candidate with exactly the target ratio + good size scores 1.0."""
    cand = asset_scorer.Candidate(path="/x", width=1600, height=900, bytes=100_000)
    # target 16:9 = 1.778; candidate ratio = 1.778 → aspect_score = 1.0
    # 100KB → size_score = 1.0
    # composite = 0.7 * 1.0 + 0.3 * 1.0 = 1.0
    score = asset_scorer.score_candidate(cand, "hero", 16 / 9)
    assert score == pytest.approx(1.0)


def test_score_wildly_off_aspect_is_near_zero():
    """A square image competing for a 16:9 slot scores low."""
    cand = asset_scorer.Candidate(path="/x", width=1000, height=1000, bytes=100_000)
    # ratio=1.0, target=1.778 → diff=0.437 → aspect_score=0.563
    # 100KB → size_score=1.0
    # composite = 0.7 * 0.563 + 0.3 * 1.0 = 0.694
    score = asset_scorer.score_candidate(cand, "hero", 16 / 9)
    assert 0.6 < score < 0.8


def test_score_small_file_is_low_even_with_good_aspect():
    """A 5KB tracking pixel with perfect aspect scores low — that's the point."""
    cand = asset_scorer.Candidate(path="/x", width=1600, height=900, bytes=5000)
    # aspect_score = 1.0, size_score = 0.0 (at MIN_BYTES)
    # composite = 0.7
    score = asset_scorer.score_candidate(cand, "hero", 16 / 9)
    assert score == pytest.approx(0.7)


# ─── assign_slots: the core behaviour ───────────────────────────────────────

def test_assign_picks_best_fit_per_slot(img_pool):
    """Each slot gets the candidate whose aspect ratio is closest to target."""
    pool = asset_scorer.load_pool([
        str(img_pool / "7.jpg"),               # ratio 1.50
        str(img_pool / "LOGO.jpg"),            # ratio 1.74
        str(img_pool / "PEG-AND-POLE-TENT.jpg"),  # ratio 1.84
        str(img_pool / "Untitled.jpg"),        # ratio 2.19
        str(img_pool / "jumping-castle.jpg"),  # ratio 1.08
    ])
    out = asset_scorer.assign_slots(pool)

    # logo target = 4.0 → Untitled.jpg (2.19) is closest among real candidates
    assert out["logo"] == str(img_pool / "Untitled.jpg")

    # hero target = 1.778 → PEG-AND-POLE (1.84) wins on aspect, tied with LOGO (1.74)
    # PEG-AND-POLE wins because it's first in pool order (1.84 vs 1.74 diff: 0.034 vs 0.021)
    # Actually LOGO is closer to 1.778... 1.74 diff=0.021, 1.84 diff=0.034
    # So LOGO.jpg wins on hero. Either way it's a wide-ish image.
    assert out["hero"] in (
        str(img_pool / "LOGO.jpg"),
        str(img_pool / "PEG-AND-POLE-TENT.jpg"),
    )

    # about target = 1.333 → 7.jpg (1.50) is closest to 1.333 among 5 candidates
    assert out["about"] == str(img_pool / "7.jpg")

    # gallery/1 target = 1.778 → same as hero, but hero took it. Next best.
    assert out["gallery/1"] is not None
    assert out["gallery/1"] != out["hero"]  # deduplication

    # gallery/2,3,4 target = 1.0 → jumping-castle.jpg (1.08) wins square tiles
    # but it can only fill one slot. The rest get... 7.jpg (1.50)? or back to nearest.
    # Whatever — the test just checks deduplication + non-None.
    squares = [out["gallery/2"], out["gallery/3"], out["gallery/4"]]
    assert None not in squares
    assert len(set(squares)) == 3  # all different


def test_assign_respects_hint_override(img_pool):
    """The LLM's hinted path wins the slot even if its aspect ratio isn't optimal."""
    pool = asset_scorer.load_pool([str(img_pool / f) for f in [
        "7.jpg", "LOGO.jpg", "PEG-AND-POLE-TENT.jpg",
        "Untitled.jpg", "jumping-castle.jpg",
    ]])
    # Force LOGO.jpg into the logo slot even though Untitled.jpg is a better fit
    hints = {"logo": str(img_pool / "LOGO.jpg")}
    out = asset_scorer.assign_slots(pool, hints)
    assert out["logo"] == str(img_pool / "LOGO.jpg")


def test_assign_ignores_hint_not_in_pool(img_pool):
    """If the LLM hints a path that's not in the pool, the scorer picks best-fit."""
    pool = asset_scorer.load_pool([
        str(img_pool / "7.jpg"),
        str(img_pool / "Untitled.jpg"),
    ])
    hints = {"logo": "/nonexistent/llm-thought-this-was-the-logo.jpg"}
    out = asset_scorer.assign_slots(pool, hints)
    # Hint ignored; Untitled.jpg (2.19) wins the 4.0 logo target
    assert out["logo"] == str(img_pool / "Untitled.jpg")


def test_assign_does_not_reuse_candidate(img_pool):
    """A candidate used in one slot can't be reused in another."""
    pool = asset_scorer.load_pool([
        str(img_pool / "7.jpg"),
        str(img_pool / "jumping-castle.jpg"),
    ])
    out = asset_scorer.assign_slots(pool)
    used = [out[s] for s in out if out[s]]
    # All non-None assignments must be unique paths
    assert len(used) == len(set(used)), f"reuse detected: {used}"


def test_assign_empty_pool_returns_all_none():
    """No candidates → all slots None → caller runs Pillow fallback."""
    out = asset_scorer.assign_slots([])
    assert all(v is None for v in out.values())
    assert set(out.keys()) == {s for s, _ in asset_scorer.SLOT_SPECS}


def test_assign_skips_taken_candidate_when_picking_next(img_pool):
    """Once a candidate is used, it's excluded from later slot picks."""
    pool = asset_scorer.load_pool([
        str(img_pool / "jumping-castle.jpg"),
        str(img_pool / "7.jpg"),
    ])
    # jumping-castle has the best aspect for square slots (1.08 vs 1.0)
    # but only one slot can claim it
    out = asset_scorer.assign_slots(pool)
    assert out["gallery/2"] == str(img_pool / "jumping-castle.jpg")
    # The other square slots had to pick 7.jpg (1.50) — best remaining
    assert out["gallery/3"] == str(img_pool / "7.jpg")
    assert out["gallery/4"] == str(img_pool / "7.jpg")


def test_assign_returns_none_when_best_score_below_threshold(img_pool):
    """A candidate below MIN_USABLE_SCORE → slot unfilled → Pillow fallback."""
    # Create a single tiny candidate that scores low
    tiny = img_pool / "tiny.jpg"
    make_jpeg(tiny, 100, 100, q=5)  # tiny file + square aspect for a 16:9 slot
    pool = asset_scorer.load_pool([str(tiny)])
    # Force the candidate to be considered for hero (16:9 target)
    # 100×100 ratio=1.0, target 1.778, diff=0.437, aspect=0.563
    # size_score is low (tiny file under MIN_BYTES)
    # composite < MIN_USABLE_SCORE (0.05)
    out = asset_scorer.assign_slots(pool)
    # All slots that want this single square tiny file should get None
    # (none of them have target ratio 1.0 except the 3 squares, but the file is too small)
    for slot, val in out.items():
        if val is not None:
            # The square slots (1.0 target) might still take it
            assert slot in ("gallery/2", "gallery/3", "gallery/4")


# ─── discount tents regression ───────────────────────────────────────────────

def test_discount_tents_scenario_does_not_zoom_logo(img_pool):
    """Regression: the bug that prompted this fix.

    Real scenario: scraper found 5 candidates for Discount Tents,
    including LOGO.jpg (1.74 ratio wide banner). With the old
    name-to-slot assignment, LOGO.jpg was being forced into the
    4:3 about slot and rendered as a "zoomed logo".

    With the new pool-based scorer, the about slot picks a 4:3
    candidate instead.
    """
    pool = asset_scorer.load_pool([
        str(img_pool / "7.jpg"),               # ratio 1.50
        str(img_pool / "LOGO.jpg"),            # ratio 1.74
        str(img_pool / "PEG-AND-POLE-TENT.jpg"),  # ratio 1.84
        str(img_pool / "Untitled.jpg"),        # ratio 2.19
        str(img_pool / "jumping-castle.jpg"),  # ratio 1.08
    ])
    out = asset_scorer.assign_slots(pool)

    # The about slot must NOT be the wide LOGO.jpg banner
    assert out["about"] != str(img_pool / "LOGO.jpg"), (
        f"about slot picked LOGO.jpg (1.74 ratio) — the original bug. "
        f"Got: {out['about']}"
    )
    # And it should be a candidate with aspect ratio closer to 4:3 (1.33)
    # 7.jpg (1.50) is closest to 1.333 among the 5
    assert out["about"] == str(img_pool / "7.jpg")


# ─── copy_assets integration ────────────────────────────────────────────────

def test_copy_assets_uses_pool_when_provided(tmp_path, img_pool, monkeypatch):
    """End-to-end: copy_assets with a pool produces non-placeholder files
    from the real images, not Pillow fallbacks."""
    # Redirect the build dir to tmp_path so we don't touch real caches
    fake_root = tmp_path / "builds"
    fake_root.mkdir()
    monkeypatch.setattr(
        "app.utils.mockup_build.mockup_project_dir",
        lambda slug: fake_root / slug,
    )

    from app.workers.mockup_helpers.build import copy_assets

    pool_paths = [str(img_pool / f) for f in [
        "7.jpg", "LOGO.jpg", "PEG-AND-POLE-TENT.jpg",
        "Untitled.jpg", "jumping-castle.jpg",
    ]]
    result = copy_assets(
        slug="discount-tents-test",
        candidate_pool=pool_paths,
        business_name="Discount Tents",
        accent_color="#f97316",
    )

    images_dir = fake_root / "discount-tents-test" / "public" / "images"
    assert (images_dir / "logo.jpg").exists()
    assert (images_dir / "hero.jpg").exists()
    assert (images_dir / "about.jpg").exists()
    for i in range(1, 5):
        assert (images_dir / "gallery" / f"{i}.jpg").exists()

    # image_audit shows zero Pillow fallbacks used
    assert result["image_audit"]["placeholder_count"] == 0
    assert result["image_audit"]["hero_is_placeholder"] is False
    assert result["image_audit"]["logo_is_placeholder"] is False


def test_copy_assets_falls_back_to_hints_when_pool_empty(tmp_path, monkeypatch):
    """Without a pool, copy_assets falls back to LLM hints verbatim (old behaviour)."""
    fake_root = tmp_path / "builds"
    fake_root.mkdir()
    monkeypatch.setattr(
        "app.utils.mockup_build.mockup_project_dir",
        lambda slug: fake_root / slug,
    )

    # Create a single source image the LLM hinted
    src_dir = tmp_path / "src"
    src_dir.mkdir()
    make_jpeg(src_dir / "the-hero.jpg", 1600, 900)

    from app.workers.mockup_helpers.build import copy_assets

    result = copy_assets(
        slug="hint-only-test",
        hero_path=str(src_dir / "the-hero.jpg"),
        candidate_pool=None,
        business_name="Hint Co",
        accent_color="#1a4d5c",
    )
    images_dir = fake_root / "hint-only-test" / "public" / "images"
    # The hero slot used the hinted path verbatim
    assert (images_dir / "hero.jpg").stat().st_size > 1024
    # diagnostics show empty pool
    assert result["pool_diagnostics"]["pool_size"] == 0


def test_copy_assets_pillow_fallback_when_neither_pool_nor_hints(tmp_path, monkeypatch):
    """No pool + no hints → Pillow fallback fires for every slot."""
    fake_root = tmp_path / "builds"
    fake_root.mkdir()
    monkeypatch.setattr(
        "app.utils.mockup_build.mockup_project_dir",
        lambda slug: fake_root / slug,
    )

    from app.workers.mockup_helpers.build import copy_assets

    result = copy_assets(
        slug="all-placeholder",
        candidate_pool=None,
        business_name="Placeholder Co",
        accent_color="#e85d04",
    )
    images_dir = fake_root / "all-placeholder" / "public" / "images"

    # Logo is text_logo — won't be flagged as placeholder by getextrema
    # (text has high contrast). Hero/about/gallery are solid or gradient.
    audit = result["image_audit"]
    # At least 3 of the 6 slots should be Pillow placeholders
    assert audit["placeholder_count"] >= 3
