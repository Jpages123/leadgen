"""Deploy helper — wrangler pages deploy + DNS A record."""
from __future__ import annotations

import hashlib
import time
from pathlib import Path

from app.utils.cloudflare_deploy import _create_dns_record_for_project, deploy_pages
from app.utils.mockup_build import mockup_project_dir


def _shorten_slug(slug: str, max_pages_name: int = 58, max_dns_label: int = 60) -> str:
    """Derive a deterministic short slug that fits both CF Pages (58) and DNS (63).

    Cloudflare Pages project names are capped at 58 chars; DNS labels at 63.
    For long slugs we keep a prefix and append a 7-char sha1 hash so that the
    demo-router Worker (which reconstructs project_name from the DNS subdomain
    by stripping 'demo-' and appending '-demo') still routes correctly.
    """
    suffix = "demo"
    if len(f"{slug}-{suffix}") <= max_pages_name and len(f"demo-{slug}") <= max_dns_label:
        return slug
    h = hashlib.sha1(slug.encode("utf-8")).hexdigest()[:7]
    keep = max_pages_name - len(suffix) - 1 - len(h) - 1  # '-' + hash
    return slug[:keep].rstrip("-") + "-" + h


def deploy(slug: str, build_dir: str = "") -> dict:
    """Full deploy: Pages + DNS. Returns the clientcompass.co.za demo URL."""
    if not build_dir:
        build_dir = str(mockup_project_dir(slug))
    project = Path(build_dir) / slug
    dist = project / "dist"
    if not dist.exists():
        raise RuntimeError(f"no dist/ dir at {dist} — did you build first?")

    short_slug = _shorten_slug(slug)
    project_name = f"{short_slug}-demo"
    dns_sub = f"demo-{short_slug}"
    demo_url = f"https://{dns_sub}.clientcompass.co.za"

    started = time.time()
    pages_url = deploy_pages(dist_dir=str(dist), project_name=project_name, cf_token=None)
    pages_ms = int((time.time() - started) * 1000)

    dns_started = time.time()
    # Need the CF token to create the DNS record manually because create_dns_record
    # expects the original slug, but our short slug produces a different DNS name.
    from app.config import get_settings
    cf_token = get_settings().cloudflare_api_token
    _create_dns_record_for_project(dns_sub=dns_sub, project_name=project_name, cf_token=cf_token)
    dns_ms = int((time.time() - dns_started) * 1000)

    return {
        "pages_url": pages_url,
        "demo_url": demo_url,
        "project_name": project_name,
        "pages_ms": pages_ms,
        "dns_ms": dns_ms,
        "total_ms": pages_ms + dns_ms,
    }
