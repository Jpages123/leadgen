"""Build pipeline helpers — clone, copy assets, pnpm build."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
from pathlib import Path

from app.utils.placeholder_image import text_logo, gradient_image, solid_image
from PIL import Image

# Template source — single Astro project with all 3 hero variants
# (cc-site-template at /tmp/template-check on the laptop, bind-mounted into
# the worker container at /opt/templates/cc-site-template).
#
# Why one repo with variants:
# - Phase E shipped 3 hero components (HeroCreative, HeroGeneral, HeroTrades)
#   inside ONE template, conditionally rendered based on brand.template.
# - The worker container has no ssh/git access to GitHub for cloning, so we
#   copy from a local path.
TEMPLATE_SOURCE_DIR = "/opt/templates/cc-site-template"
TEMPLATE_VARIANTS = {"trades", "creative", "general"}  # all live in same dir


def project_dir_for(slug: str, build_dir: str) -> Path:
    return Path(build_dir) / slug


def clone_template(template: str, slug: str, build_dir: str = "/tmp/cc_mockups") -> dict:
    """Copy the cc-site-template into the build dir.

    The template has all 3 hero variants (HeroCreative/HeroGeneral/HeroTrades)
    in src/components/. The `template` arg here is informational — the LLM
    chooses which hero to render by setting `brand.template` in brand.ts.
    """
    if template not in TEMPLATE_VARIANTS:
        raise ValueError(f"unknown template: {template}. choices: {sorted(TEMPLATE_VARIANTS)}")

    src_dir = Path(TEMPLATE_SOURCE_DIR)
    if not src_dir.exists():
        raise FileNotFoundError(
            f"template source not found at {src_dir}. "
            f"Make sure the cc-site-template is bind-mounted into the container."
        )

    dest = project_dir_for(slug, build_dir)
    if dest.exists():
        shutil.rmtree(dest)
    Path(build_dir).mkdir(parents=True, exist_ok=True)

    started = time.time()
    # Copy with symlinks=False so each mockup is independent.
    # Skip node_modules + dist + .cache to save space/time.
    def _ignore(dir, files):
        skip = {"node_modules", "dist", ".cache", ".astro", ".git", "__pycache__"}
        return [f for f in files if f in skip]

    shutil.copytree(src_dir, dest, ignore=_ignore, symlinks=False)
    elapsed_ms = int((time.time() - started) * 1000)

    return {"path": str(dest), "template": template, "clone_ms": elapsed_ms}


def copy_assets(
    slug: str,
    logo_path: str | None,
    hero_path: str | None,
    gallery_paths: list[str],
    build_dir: str = "/tmp/cc_mockups",
    business_name: str = "Business",
    accent_color: str = "#1a4d5c",
) -> dict:
    """Copy scraped images into the template's public/images/ dir.

    Falls back to Pillow-generated placeholders when source files are missing.
    Validates file sizes (>1KB) to refuse to deploy zero-byte images.
    """
    project = project_dir_for(slug, build_dir)
    images_dir = project / "public" / "images"
    gallery_dir = images_dir / "gallery"
    images_dir.mkdir(parents=True, exist_ok=True)

    copied: list[dict] = []

    def _save(src: str | None, dest: Path, kind: str, fallback_fn=None) -> int | None:
        """Copy src to dest, or run fallback_fn(dest) if src is missing/zero-byte."""
        if src and Path(src).exists():
            try:
                # Use PIL to normalize to JPEG (handles PNG/WEBP/etc)
                img = Image.open(src).convert("RGB")
                img.save(dest, "JPEG", quality=85)
                size = dest.stat().st_size
                if size >= 1024:
                    copied.append({"kind": kind, "src": src, "dest": str(dest), "bytes": size, "fallback": False})
                    return size
            except Exception:
                pass  # fall through to fallback
        if fallback_fn:
            fallback_fn(dest)
            size = dest.stat().st_size
            copied.append({"kind": kind, "src": "fallback", "dest": str(dest), "bytes": size, "fallback": True})
            return size
        return None

    # Logo
    logo_size = _save(
        logo_path, images_dir / "logo.jpg", "logo",
        fallback_fn=lambda d: text_logo(business_name, accent_color, d),
    )

    # Hero — fall back to gradient if no hero
    hero_size = _save(
        hero_path, images_dir / "hero.jpg", "hero",
        fallback_fn=lambda d: gradient_image(accent_color, d, label=business_name),
    )

    # About — reuse hero if no separate about image
    about_size = _save(
        hero_path, images_dir / "about.jpg", "about",
        fallback_fn=lambda d: gradient_image(accent_color, d, label=business_name),
    )

    # Gallery
    if gallery_paths:
        gallery_dir.mkdir(parents=True, exist_ok=True)
        for i, g_path in enumerate(gallery_paths[:4], 1):
            _save(
                g_path, gallery_dir / f"{i}.jpg", f"gallery/{i}",
                fallback_fn=lambda d, idx=i: solid_image(accent_color, d, label=f"Photo {idx}"),
            )

    # Validate
    required = [images_dir / "logo.jpg", images_dir / "hero.jpg", images_dir / "about.jpg"]
    missing_or_zero = [
        {"path": str(p), "size": p.stat().st_size if p.exists() else 0}
        for p in required if not p.exists() or p.stat().st_size < 1024
    ]
    if missing_or_zero:
        raise RuntimeError(f"required images missing or zero-byte: {missing_or_zero}")

    return {
        "copied": copied,
        "logo_bytes": logo_size,
        "hero_bytes": hero_size,
        "about_bytes": about_size,
        "gallery_count": sum(1 for c in copied if c["kind"].startswith("gallery/")),
    }


def build(slug: str, build_dir: str = "/tmp/cc_mockups", timeout_s: int = 180) -> dict:
    """Run pnpm install + pnpm build in the cloned template."""
    project = project_dir_for(slug, build_dir)
    if not (project / "package.json").exists():
        raise RuntimeError(f"no package.json in {project} — was the template cloned?")

    started = time.time()
    # pnpm install
    proc = subprocess.run(
        ["pnpm", "install", "--prefer-offline"],
        cwd=project, capture_output=True, text=True, timeout=timeout_s,
    )
    install_ms = int((time.time() - started) * 1000)
    if proc.returncode != 0:
        raise RuntimeError(
            f"pnpm install failed (exit {proc.returncode}, {install_ms}ms): {proc.stderr[-400:]}"
        )

    # pnpm build
    build_started = time.time()
    proc = subprocess.run(
        ["pnpm", "build"],
        cwd=project, capture_output=True, text=True, timeout=timeout_s,
    )
    build_ms = int((time.time() - build_started) * 1000)
    if proc.returncode != 0:
        raise RuntimeError(
            f"pnpm build failed (exit {proc.returncode}, {build_ms}ms): {proc.stderr[-400:]}"
        )

    dist = project / "dist"
    if not dist.exists():
        raise RuntimeError(f"pnpm build succeeded but no dist/ dir at {dist}")

    # Validate dist/ has expected files
    index_html = dist / "index.html"
    images_dist = dist / "images"
    if not index_html.exists():
        raise RuntimeError(f"dist/index.html missing")
    if not images_dist.exists():
        raise RuntimeError(f"dist/images/ missing")

    # Check image sizes in dist/
    dist_image_issues = []
    for img in (images_dist).glob("*.jpg"):
        if img.stat().st_size < 1024:
            dist_image_issues.append({"file": str(img), "size": img.stat().st_size})
    if dist_image_issues:
        raise RuntimeError(f"dist/ has zero-byte images: {dist_image_issues}")

    total_ms = int((time.time() - started) * 1000)
    return {
        "install_ms": install_ms,
        "build_ms": build_ms,
        "total_ms": total_ms,
        "dist_path": str(dist),
        "dist_size_kb": sum(f.stat().st_size for f in dist.rglob("*") if f.is_file()) // 1024,
    }
