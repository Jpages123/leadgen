"""Durable storage location for audit-scraped brand assets.

Background
----------
Until 2026-07-09, audits wrote scraped assets (logo, hero, gallery images,
homepage screenshot) to ``/tmp/cc_audits/<slug>-*.{jpg,png}`` — a path
guaranteed to disappear at the next reboot, container rebuild, or even
``/tmp`` cleanup. The mockup generator relied on those paths being present
at build time, which led to silent deployment of zero-byte placeholder
images whenever an audit ran more than a few hours before the mockup build
(see Session 9 log).

This module is the single source of truth for where those assets live:

  1. New audits write to ``<project>/.cache/audits/<slug>-*.{jpg,png}`` —
     durable, bind-mounted into the worker container, gitignored.
  2. The mockup generator's ``_resolve_asset`` helper checks this location
     first, then falls back to ``/tmp/cc_audits/`` for any pre-fix audits
     still in flight.
  3. Operators can prune ``.cache/audits/`` safely — assets can always
     be re-scraped by re-running the audit task.

The filename convention is preserved from the legacy layout
(``<slug>-<kind>.<ext>``) so the existing scraper and existing DB rows
work unchanged — only the base directory moves.

Public API
----------
- ``audit_assets_dir()`` — the durable base directory
- ``asset_path(slug, kind, ext=".jpg")`` — absolute path for a specific asset
- ``resolve(slug, kind, ext=".jpg")`` — finds the file in either new or
  legacy location; returns ``None`` if missing in both
"""
from __future__ import annotations

from pathlib import Path

# Legacy location — used only for migration lookups. Will be removed once
# we trust that no in-flight audits still write here.
_LEGACY_AUDIT_DIR = Path("/tmp/cc_audits")


def audit_assets_dir() -> Path:
    """Return the durable base directory for audit assets.

    Resolved relative to the project root (parent of ``app/``).
    Created on first call; safe to call repeatedly.
    """
    project_root = Path(__file__).resolve().parent.parent.parent
    base = project_root / ".cache" / "audits"
    base.mkdir(parents=True, exist_ok=True)
    return base


def asset_path(slug: str, kind: str, ext: str = ".jpg") -> Path:
    """Return the durable path for a specific asset.

    Layout: ``<base>/<slug>-<kind>.<ext>`` — preserved from the legacy
    scraper so audit rows in the DB keep working.

    Args:
        slug: URL slug for the lead (e.g. ``datra-construction``).
        kind: One of ``screenshot``, ``logo``, ``hero``, ``gallery-1`` …
              ``gallery-N``.
        ext: File extension including the leading dot.

    Examples::

        asset_path("datra-construction", "screenshot")  -> .cache/audits/datra-construction-screenshot.jpg
        asset_path("datra-construction", "gallery-1")   -> .cache/audits/datra-construction-gallery-1.jpg
        asset_path("datra-construction", "logo", ".png")-> .cache/audits/datra-construction-logo.png
    """
    if not ext.startswith("."):
        ext = "." + ext
    return audit_assets_dir() / f"{slug}-{kind}{ext}"


def resolve(slug: str, kind: str, ext: str = ".jpg") -> Path | None:
    """Find the asset in the durable location, then the legacy ``/tmp`` location.

    Returns ``None`` if missing in both. Used by the mockup generator to
    read scraped assets without caring whether the audit predates the fix.
    """
    new = asset_path(slug, kind, ext)
    if new.exists() and new.stat().st_size > 0:
        return new

    # Legacy lookup — pre-fix audits wrote to /tmp/cc_audits/<slug>-<kind>.<ext>.
    # We don't know the exact extension the legacy audit used, so probe a few.
    legacy_base = _LEGACY_AUDIT_DIR / f"{slug}-{kind}"
    for legacy_ext in (".jpg", ".jpeg", ".png", ".webp"):
        candidate = legacy_base.with_suffix(legacy_ext)
        if candidate.exists() and candidate.stat().st_size > 0:
            return candidate

    return None