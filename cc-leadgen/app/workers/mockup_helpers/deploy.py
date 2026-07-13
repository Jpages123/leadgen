"""Deploy helper — wrangler pages deploy + DNS A record."""
from __future__ import annotations

import os
import time
from pathlib import Path

from app.utils.cloudflare_deploy import deploy_pages, create_dns_record


def deploy(slug: str, build_dir: str = "/tmp/cc_mockups") -> dict:
    """Full deploy: Pages + DNS. Returns the clientcompass.co.za demo URL."""
    project = Path(build_dir) / slug
    dist = project / "dist"
    if not dist.exists():
        raise RuntimeError(f"no dist/ dir at {dist} — did you build first?")

    project_name = f"{slug}-demo"
    started = time.time()

    pages_url = deploy_pages(dist_dir=str(dist), project_name=project_name, cf_token=None)
    pages_ms = int((time.time() - started) * 1000)

    dns_started = time.time()
    demo_url = create_dns_record(slug=slug, cf_token=None)
    dns_ms = int((time.time() - dns_started) * 1000)

    return {
        "pages_url": pages_url,
        "demo_url": demo_url,
        "project_name": project_name,
        "pages_ms": pages_ms,
        "dns_ms": dns_ms,
        "total_ms": pages_ms + dns_ms,
    }
