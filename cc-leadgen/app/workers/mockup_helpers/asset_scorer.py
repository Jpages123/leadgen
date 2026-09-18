"""Pool-based slot-fit scoring for mockup image selection.

Background
----------
Session 21 (2026-08-05) found that the previous copy_assets() did blind
name-to-slot assignment: the LLM in the mockup-builder.ts skill got only
file paths from the scraper (no pixels) and passed them straight through
to ``copy_assets``. When the scraper missed images — common with
SitePad, Wix, and other non-standard builders — Pillow placeholders
filled the gaps. And when the scraper DID capture images, the LLM
sometimes picked the wrong slot for them (e.g. Discount Tents' wide
"LOGO.jpg" banner was forced into a 4:3 about slot, producing a
"zoomed logo" look).

This module fixes both failure modes by:
  1. Scoring every candidate by aspect-ratio fit + file-size signal
  2. Letting the LLM's path hints act as a +100 boost (override) rather
     than a hard requirement — if the hint isn't in the pool, the
     scorer picks the best-fit instead
  3. Deduplicating — once a candidate is used in one slot, it can't be
     reused in another (prevents "same photo 4 times in gallery")

Pool contract
-------------
A "candidate pool" is just a list of file paths that exist on disk and
are > 1KB (the same threshold the validator uses). The scorer probes
each path's dimensions + size at call time, so it doesn't need a
pre-loaded manifest.

Slot specs
----------
The 7 mockup image slots are described by a fixed list of (name, target
aspect ratio) pairs. The scorer picks one candidate per slot.

Backward compat
---------------
``assign_slots()`` returns ``None`` for slots it couldn't fill from the
pool — the caller is expected to run Pillow fallback in that case.
When the pool is empty, every slot is None and the caller falls back
to pure hint-based assignment (the pre-fix behaviour).
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from PIL import Image, UnidentifiedImageError


# Target aspect ratios (W/H) per mockup slot. Match what the template
# renders: logo is 480×120 (text_logo default), hero & featured gallery
# are 16:9, about is 4:3, square tiles are 1:1.
SLOT_SPECS: list[tuple[str, float]] = [
    ("logo", 4.0),         # 480×120 baseline
    ("hero", 16 / 9),      # 1.778
    ("about", 4 / 3),      # 1.333
    ("gallery/1", 16 / 9), # 1.778 — featured slot, wider is better
    ("gallery/2", 1.0),    # square
    ("gallery/3", 1.0),    # square
    ("gallery/4", 1.0),    # square
]

# Weights for the composite score. Aspect ratio dominates (the failure
# mode this fixes was "wrong-aspect image forced into slot"); file size
# is a tiebreaker for "is this a real photo or a 2KB tracking pixel".
ASPECT_WEIGHT = 0.7
SIZE_WEIGHT = 0.3

# Hinted candidates get a +100 boost, which is effectively a hard override.
HINT_BOOST = 100.0

# If the highest-scoring candidate for a slot is below this threshold,
# the slot is left unfilled (caller runs Pillow fallback).
MIN_USABLE_SCORE = 0.05

# File-size signal: 0 at MIN_BYTES, 1.0 at SATURATION_BYTES.
MIN_BYTES = 5_000        # below this is almost certainly a placeholder/1x1
SATURATION_BYTES = 60_000  # at/above this is a full-quality real photo


@dataclass(frozen=True)
class Candidate:
    """A pool entry — file path + probed dimensions + bytes."""
    path: str
    width: int
    height: int
    bytes: int

    @property
    def ratio(self) -> float:
        return self.width / self.height if self.height else 1.0


def _probe(path: str) -> Candidate | None:
    """Open the file and return a Candidate, or None if unusable.

    Unusable = file missing, zero-byte, too-small, or undecodable as
    an image. The 1KB floor mirrors copy_assets's pre-existing
    validator threshold (zero-byte reject).
    """
    try:
        p = Path(path)
        size = p.stat().st_size
        if size < 1024:
            return None
        with Image.open(p) as img:
            w, h = img.size
            if w <= 0 or h <= 0:
                return None
        return Candidate(path=path, width=w, height=h, bytes=size)
    except (OSError, UnidentifiedImageError):
        return None


def load_pool(paths: Iterable[str]) -> list[Candidate]:
    """Probe every path; return only usable candidates.

    Order is preserved (a path that probed fine stays in its original
    slot). Duplicates are removed by path string.
    """
    seen: set[str] = set()
    out: list[Candidate] = []
    for raw in paths:
        if not raw or raw in seen:
            continue
        cand = _probe(raw)
        if cand is not None:
            seen.add(raw)
            out.append(cand)
    return out


def _aspect_score(candidate_ratio: float, target_ratio: float) -> float:
    """How well does this candidate's ratio fit the target? 1.0 = perfect.

    Uses relative difference so a 0.5 diff at target=1.0 (candidate=1.5
    or 0.5) scores 0.5, and a 0.5 diff at target=4.0 (candidate=2.0 or
    6.0) also scores 0.5. Clamped to [0, 1].
    """
    if target_ratio <= 0:
        return 0.0
    diff = abs(candidate_ratio - target_ratio) / target_ratio
    return max(0.0, 1.0 - min(diff, 1.0))


def _size_score(n_bytes: int) -> float:
    """Linear ramp: 0 at MIN_BYTES, 1.0 at SATURATION_BYTES, clamped."""
    if n_bytes <= MIN_BYTES:
        return 0.0
    if n_bytes >= SATURATION_BYTES:
        return 1.0
    return (n_bytes - MIN_BYTES) / (SATURATION_BYTES - MIN_BYTES)


def score_candidate(cand: Candidate, slot_name: str, target_ratio: float) -> float:
    """Composite score for placing ``cand`` into the named slot."""
    return (
        ASPECT_WEIGHT * _aspect_score(cand.ratio, target_ratio)
        + SIZE_WEIGHT * _size_score(cand.bytes)
    )


def assign_slots(
    pool: list[Candidate],
    hints: dict[str, str | None] | None = None,
    slot_specs: list[tuple[str, float]] | None = None,
) -> dict[str, str | None]:
    """Pick the best candidate for each slot, respecting hints.

    Args:
        pool: Candidates loaded from disk by ``load_pool()``. Order is
            the priority for tie-breaking — first candidate wins ties.
        hints: Optional map of slot name → candidate path the LLM
            explicitly suggested. The hint wins if (a) it's in the pool
            and (b) it probed OK.
        slot_specs: Override the default slot list. Useful for tests.

    Returns:
        Dict mapping slot name to assigned candidate path, or None
        for slots that should fall back to Pillow.
    """
    hints = hints or {}
    specs = slot_specs if slot_specs is not None else SLOT_SPECS

    # Map paths → candidates for O(1) hint lookup
    pool_by_path: dict[str, Candidate] = {c.path: c for c in pool}
    taken: set[str] = set()
    out: dict[str, str | None] = {}

    for slot_name, target_ratio in specs:
        best_path: str | None = None
        best_score = -1.0

        # The hinted path gets a free pass into the top spot if it exists
        # in the pool — we still respect the "taken" guard so we don't
        # reuse the same candidate for multiple slots.
        hinted_path = hints.get(slot_name)
        if hinted_path and hinted_path in pool_by_path and hinted_path not in taken:
            best_path = hinted_path
            best_score = HINT_BOOST
        else:
            for cand in pool:
                if cand.path in taken:
                    continue
                score = score_candidate(cand, slot_name, target_ratio)
                if score > best_score:
                    best_score = score
                    best_path = cand.path

        if best_path is not None and best_score >= MIN_USABLE_SCORE:
            out[slot_name] = best_path
            taken.add(best_path)
        else:
            out[slot_name] = None

    return out
