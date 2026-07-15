"""Durable storage + content-hash skip-rebuild for mockup build dirs.

Background
----------
Until 2026-07-15, the mockup build pipeline wrote every artifact to
``/tmp/cc_mockups/<slug>/`` — a path wiped on every container restart,
rebuild, or even ``/tmp`` cleanup. Each build paid the full cost from
scratch:

  1. ``shutil.rmtree(dest)`` (line ~46 of build.py) wiped the entire
     project dir including ``node_modules/`` (the most expensive step,
     ~30-60s of ``pnpm install``)
  2. Template cloned again (~3s)
  3. ``pnpm install --prefer-offline`` (~30-60s, the bottleneck)
  4. ``pnpm build`` (~10-30s)
  5. Deploy (~2s)

The user explicitly flagged this as wasteful — mockups were being
rebuilt from scratch every time even when the inputs (business name,
brand color, scraped images) hadn't changed.

This module introduces:

  1. **Durable build dir** at ``<project>/.cache/mockups/<slug>/`` —
     bind-mounted via the existing ``.:/app`` volume, gitignored, so
     ``node_modules/`` + ``dist/`` survive container restarts.
  2. **Content-hash skip-rebuild** — a SHA-256 of the inputs that
     affect the build output (``client.ts``, ``brand.ts``, scraped
     assets) is stored alongside ``dist/`` as ``.build_hash``. If the
     hash matches, the rebuild step is a no-op and the existing
     ``dist/`` is reused.
  3. **Cache invalidation** — when inputs change, the hash differs
     and the build runs cleanly (re-install if lockfile changed, else
     rebuild in place without wiping ``node_modules/``).

Public API
----------
- ``mockup_builds_dir()`` — durable base directory
- ``mockup_project_dir(slug)`` — ``<dir>/<slug>/``
- ``compute_input_hash(client_ts, brand_ts, asset_paths)`` — SHA-256
  of all build-affecting inputs
- ``read_cached_hash(slug)`` — the hash stored at ``.build_hash`` if
  the last build succeeded, else ``None``
- ``write_build_hash(slug, h)`` — called by the build pipeline on
  success
- ``should_skip_build(slug, client_ts, brand_ts, asset_paths)`` —
  convenience wrapper around the above
"""
from __future__ import annotations

import hashlib
from pathlib import Path

from app.utils.logger import get_logger

log = get_logger(__name__)

HASH_FILENAME = ".build_hash"


def mockup_builds_dir() -> Path:
    """Return the durable base directory for mockup build dirs.

    Resolved relative to the project root (parent of ``app/``).
    Created on first call; safe to call repeatedly.

    Bind-mounted into the worker container via the existing ``.:/app``
    volume in ``docker-compose.yml``, so ``node_modules/`` and ``dist/``
    survive container restarts.
    """
    project_root = Path(__file__).resolve().parent.parent.parent
    base = project_root / ".cache" / "mockups"
    base.mkdir(parents=True, exist_ok=True)
    return base


def mockup_project_dir(slug: str) -> Path:
    """Return the durable build dir for a specific mockup.

    Args:
        slug: Business-name slug (matches the existing convention in
              ``build.project_dir_for``).

    Returns:
        Absolute path under ``<project>/.cache/mockups/<slug>/``.
    """
    return mockup_builds_dir() / slug


def compute_input_hash(
    client_ts: str,
    brand_ts: str,
    asset_paths: list[str | None],
) -> str:
    """SHA-256 hash of all inputs that affect the build output.

    Includes:
      - client.ts and brand.ts content (the LLM-generated config)
      - The bytes of each asset file (logo, hero, gallery images)

    Two builds with the same hash will produce identical dist/ output
    (modulo timestamps inside the static site, which the operator
    shouldn't see anyway).
    """
    h = hashlib.sha256()
    h.update(b"client.ts\n")
    h.update(client_ts.encode("utf-8"))
    h.update(b"\nbrand.ts\n")
    h.update(brand_ts.encode("utf-8"))
    h.update(b"\nassets\n")
    for p in sorted([x for x in (asset_paths or []) if x]):
        path = Path(p)
        if path.exists() and path.is_file():
            h.update(path.read_bytes())
        else:
            # Missing files contribute a sentinel so a fallback-to-Pillow
            # change (e.g. logo went from real to placeholder) is
            # detected as a hash change.
            h.update(b"<missing>")
    return h.hexdigest()


def read_cached_hash(slug: str) -> str | None:
    """Return the hash from the last successful build, or ``None``.

    Returns ``None`` if:
      - no ``.build_hash`` file exists yet (first build)
      - the build dir doesn't exist
      - the file is corrupt or empty (treated as cache miss — safe default)
    """
    hash_path = mockup_project_dir(slug) / HASH_FILENAME
    if not hash_path.exists():
        return None
    try:
        value = hash_path.read_text().strip()
        return value if value else None
    except Exception as exc:
        log.warning("build_hash_read_failed", slug=slug, error=str(exc))
        return None


def write_build_hash(slug: str, h: str) -> None:
    """Persist the input hash after a successful build.

    Called by the build pipeline once ``pnpm build`` succeeds. The
    next call to ``should_skip_build()`` will compare against this
    value.
    """
    hash_path = mockup_project_dir(slug) / HASH_FILENAME
    hash_path.parent.mkdir(parents=True, exist_ok=True)
    hash_path.write_text(h)


def should_skip_build(
    slug: str,
    client_ts: str,
    brand_ts: str,
    asset_paths: list[str | None],
) -> bool:
    """Return True if the build can be skipped entirely.

    Conditions for a skip:
      1. The build dir exists
      2. ``dist/index.html`` exists (last build succeeded)
      3. The cached hash matches the current input hash

    If any condition fails, the caller should rebuild. The function is
    conservative — it never returns True unless all three conditions
    hold, so a false-positive skip (which would deploy stale content)
    is impossible.
    """
    project = mockup_project_dir(slug)
    dist_index = project / "dist" / "index.html"
    if not project.exists() or not dist_index.exists():
        return False

    cached = read_cached_hash(slug)
    if cached is None:
        return False

    current = compute_input_hash(client_ts, brand_ts, asset_paths)
    return cached == current