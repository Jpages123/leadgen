"""Manually run the full mockup pipeline for Limelight, bypassing the skill.

Steps:
  1. Clean up any previous build dir
  2. Clone the creative template
  3. Place the 6 curated Pexels images at the correct paths
  4. Write client.ts + brand.ts directly (pre-built with correct facts)
  5. Generate a logo placeholder (Pillow text-logo)
  6. pnpm install + pnpm build
  7. Deploy to Cloudflare Pages (overwrites existing limelight-event-hire-demo)

Usage:
  docker compose exec -T worker bash -c "PYTHONPATH=/app .venv/bin/python /app/scripts/manual_build.py"
"""
from __future__ import annotations

import shutil
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, "/app")

from app.utils.placeholder_image import text_logo  # noqa: E402

SLUG = "limelight-event-hire"
BUILD_DIR = Path("/tmp/cc_mockups")
PROJECT = BUILD_DIR / SLUG
PICKED_DIR = Path("/tmp/cc_picked")  # 6 Pexels images pre-staged here

# Image filename map: target_name -> picked source filename
IMAGE_MAP = {
    "hero.jpg":          "hero.jpg",          # wedding marquee evening
    "about.jpg":         "about.jpg",         # team setting up
    "gallery/1.jpg":     "gallery-1.jpg",     # wedding table
    "gallery/2.jpg":     "gallery-2.jpg",     # dance floor (specialty)
    "gallery/3.jpg":     "gallery-3.jpg",     # fairy lights
    "gallery/4.jpg":     "gallery-4.jpg",     # corporate gala
}

BRAND_COLOR = "#c8a96a"
BUSINESS_NAME = "Limelight Event Hire"


def step(msg: str) -> None:
    print(f"\n=== {msg} ===")


def run(cmd: list[str], **kw) -> subprocess.CompletedProcess:
    print(f"  $ {' '.join(cmd[:6])}{'...' if len(cmd) > 6 else ''}")
    return subprocess.run(cmd, capture_output=True, text=True, **kw)


def main():
    started = time.time()

    step("1. Reset build dir")
    if PROJECT.exists():
        shutil.rmtree(PROJECT)
    BUILD_DIR.mkdir(parents=True, exist_ok=True)
    print(f"  cleaned {PROJECT}")

    step("2. Clone template")
    from app.workers.mockup_helpers.build import clone_template
    result = clone_template(template="creative", slug=SLUG, build_dir=str(BUILD_DIR))
    print(f"  cloned {result['template']} template to {result['path']} ({result['clone_ms']}ms)")

    step("3. Place curated Pexels images")
    images_dir = PROJECT / "public" / "images"
    gallery_dir = images_dir / "gallery"
    images_dir.mkdir(parents=True, exist_ok=True)
    gallery_dir.mkdir(parents=True, exist_ok=True)
    for target_name, src_name in IMAGE_MAP.items():
        src = PICKED_DIR / src_name
        dest = images_dir / target_name
        if not src.exists():
            print(f"  WARN: missing source {src}")
            continue
        shutil.copy(src, dest)
        print(f"  {target_name}: {dest.stat().st_size:,} bytes")

    step("4. Generate text-logo placeholder")
    logo_path = images_dir / "logo.jpg"
    text_logo(BUSINESS_NAME, BRAND_COLOR, logo_path)
    print(f"  generated logo.jpg ({logo_path.stat().st_size:,} bytes)")

    step("5. Write client.ts + brand.ts")
    # Read from the staging dir on the host
    cfg_src_dir = Path("/opt/mockup-configs/limelight")
    if not cfg_src_dir.exists():
        # Try the laptop-side path via mount or shared volume
        cfg_src_dir = Path("/tmp/mockup-configs/limelight")
    cfg_src_dir.mkdir(parents=True, exist_ok=True)

    # Write the configs
    config_dir = PROJECT / "src" / "config"
    config_dir.mkdir(parents=True, exist_ok=True)

    client_path = Path("/tmp/cc_picked/client.ts")
    brand_path = Path("/tmp/cc_picked/brand.ts")
    if not client_path.exists():
        print(f"  ERROR: {client_path} missing - copy from staging")
        return 1
    if not brand_path.exists():
        print(f"  ERROR: {brand_path} missing - copy from staging")
        return 1

    (config_dir / "client.ts").write_text(client_path.read_text())
    (config_dir / "brand.ts").write_text(brand_path.read_text())
    print(f"  client.ts: {(config_dir / 'client.ts').stat().st_size:,} bytes")
    print(f"  brand.ts:  {(config_dir / 'brand.ts').stat().st_size:,} bytes")

    step("6. Validate images (no zero-byte files)")
    for img in (images_dir).rglob("*.jpg"):
        size = img.stat().st_size
        status = "OK" if size >= 1024 else "ZERO-BYTE!"
        print(f"  {img.relative_to(PROJECT)}: {size:,} bytes [{status}]")

    step("7. pnpm install + build")
    proc = run(["pnpm", "install", "--prefer-offline"], cwd=PROJECT, timeout=180)
    if proc.returncode != 0:
        print(f"  pnpm install FAILED:\n{proc.stderr[-500:]}")
        return 1
    print(f"  pnpm install OK ({proc.stdout.count('Done in')} matches)")

    proc = run(["pnpm", "build"], cwd=PROJECT, timeout=180)
    if proc.returncode != 0:
        print(f"  pnpm build FAILED:\n{proc.stderr[-1000:]}")
        return 1
    print(f"  pnpm build OK")

    dist = PROJECT / "dist"
    if not dist.exists():
        print(f"  ERROR: dist/ missing after build")
        return 1
    dist_size_kb = sum(f.stat().st_size for f in dist.rglob("*") if f.is_file()) // 1024
    print(f"  dist/ total: {dist_size_kb} KB")

    step("8. Deploy to Cloudflare Pages")
    from app.workers.mockup_helpers.deploy import deploy
    deploy_result = deploy(slug=SLUG, build_dir=str(BUILD_DIR))
    print(f"  pages_url: {deploy_result['pages_url']}")
    print(f"  demo_url:  {deploy_result['demo_url']}")
    print(f"  deploy_ms: {deploy_result['total_ms']}ms")

    step("9. Write approval row")
    from app.db.sync_session import sync_session_scope
    from app.models.lead import Lead
    from app.workers.mockup_helpers.write_approval import write_approval
    with sync_session_scope() as session:
        lead = session.get(Lead, "eb3ec99f-b903-466a-a47d-f4cce03fe584")
        if lead:
            recommendation = {
                "template": "creative",
                "rationale": "Manual rebuild with corrected facts + curated Pexels imagery",
                "service_areas": ["Pinetown", "Durban", "Umhlanga", "Ballito", "KZN Midlands", "Drakensberg", "North Coast", "South Coast"],
            }
            approval = write_approval(
                lead_id=str(lead.id),
                mockup_url=deploy_result["demo_url"],
                recommendation=recommendation,
            )
            print(f"  approval_id: {approval['approval_id']}")
            # Update leadgen state
            from datetime import datetime, timezone
            lead.mockup_status = "pending_approval"
            lead.mockup_url = deploy_result["demo_url"]
            lead.mockup_generated_at = datetime.now(timezone.utc)
            session.add(lead)
            print(f"  lead mockup_status set to pending_approval")

    elapsed = int(time.time() - started)
    print(f"\n=== Done in {elapsed}s ===")
    print(f"Live URL: {deploy_result['demo_url']}")


if __name__ == "__main__":
    sys.exit(main() or 0)
