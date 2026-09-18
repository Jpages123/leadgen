"""Pick best 6 Pexels images for Limelight event hire company + copy to build assets."""
import shutil
import subprocess
from pathlib import Path

# Selected based on file metadata + image relevance to events company in KZN
# hero = elegant outdoor marquee evening (their hero shot)
# about = team setting up decor
# gallery 1-4 = various event types (wedding table, dance floor, fairy lights, corporate)

# Note: image dimensions vary; we'll let browser/CSS scale them to fit.
# Pexels originals are 6000-8000px wide; we use the 'large' (1280px) sized version.

SELECTED = [
    ("hero", "hero_0.jpg"),
    ("about", "about_1.jpg"),
    ("gallery-1", "gallery1_0.jpg"),
    ("gallery-2", "dance_floor_1.jpg"),
    ("gallery-3", "gallery3_4.jpg"),
    ("gallery-4", "gallery4_2.jpg"),
]

SRC_DIR = Path("/tmp/pexels_candidates")
DOCKER_TMP = Path("/tmp/cc_picked")

DOCKER_TMP.mkdir(parents=True, exist_ok=True)

for label, fname in SELECTED:
    src = SRC_DIR / fname
    dest = DOCKER_TMP / f"{label}.jpg"
    shutil.copy(src, dest)
    print(f"  {label}: {fname} -> {dest} ({dest.stat().st_size:,} bytes)")

print(f"\nTotal: {len(SELECTED)} images staged in {DOCKER_TMP}")
