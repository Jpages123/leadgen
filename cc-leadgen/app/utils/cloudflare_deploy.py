"""Cloudflare Pages deploy + DNS A record creation for mockup sites.

Wraps the wrangler CLI and Cloudflare REST API.
Each mockup gets:
  - A Cloudflare Pages project:  <slug>-demo
  - A proxied DNS A record:      demo-<slug>.clientcompass.co.za → 192.0.2.1
  - Routed by the demo-router Worker to <slug>-demo.pages.dev
"""
from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import httpx

from app.config import get_settings
from app.utils.logger import get_logger

log = get_logger(__name__)

# clientcompass.co.za Cloudflare zone ID
_CC_ZONE_ID = "d38822bebc141addb18ee5949406b6ff"
_CF_ACCOUNT_ID = "ee0ec95e295ef30d379c2c35febd6591"
_CF_API_BASE = "https://api.cloudflare.com/client/v4"


def _resolve_wrangler() -> str:
    found = shutil.which("wrangler")
    if found:
        return found
    for candidate in [
        Path.home() / ".nvm/versions/node/v23.1.0/bin/wrangler",
        Path("/usr/local/bin/wrangler"),
    ]:
        if candidate.exists():
            return str(candidate)
    raise FileNotFoundError("Cannot locate wrangler CLI")


def _build_env(cf_token: str) -> dict[str, str]:
    env = os.environ.copy()
    env["CLOUDFLARE_API_TOKEN"] = cf_token
    try:
        wrangler_path = _resolve_wrangler()
        env["PATH"] = f"{Path(wrangler_path).parent}:{env.get('PATH', '')}"
    except FileNotFoundError:
        pass
    return env



def _ensure_pages_project(project_name: str, cf_token: str) -> None:
    """Create the Cloudflare Pages project if it does not exist yet."""
    settings = get_settings()
    account_id = settings.cloudflare_account_id if hasattr(settings, "cloudflare_account_id") else "ee0ec95e295ef30d379c2c35febd6591"
    headers = {
        "Authorization": f"Bearer {cf_token}",
        "Content-Type": "application/json",
    }
    with httpx.Client(timeout=20) as client:
        # Check if already exists
        resp = client.get(
            f"{_CF_API_BASE}/accounts/{account_id}/pages/projects/{project_name}",
            headers=headers,
        )
        if resp.status_code == 200:
            log.info("pages_project_exists", project=project_name)
            return
        # Create it
        create_resp = client.post(
            f"{_CF_API_BASE}/accounts/{account_id}/pages/projects",
            headers=headers,
            json={"name": project_name, "production_branch": "main"},
        )
        create_resp.raise_for_status()
        result = create_resp.json()
        if not result.get("success"):
            raise RuntimeError(f"Pages project create failed: {result.get('errors')}")
    log.info("pages_project_created", project=project_name)


def deploy_pages(dist_dir: str, project_name: str, cf_token: str) -> str:
    """Deploy dist/ to Cloudflare Pages. Returns the live pages.dev URL."""
    wrangler = _resolve_wrangler()
    env = _build_env(cf_token)

    _ensure_pages_project(project_name=project_name, cf_token=cf_token)

    result = subprocess.run(
        [
            wrangler, "pages", "deploy", dist_dir,
            "--project-name", project_name,
            "--branch", "main",
            "--commit-dirty=true",
        ],
        capture_output=True,
        text=True,
        env=env,
        timeout=120,
    )

    if result.returncode != 0:
        raise RuntimeError(
            f"wrangler pages deploy failed (exit {result.returncode}): {result.stderr[:500]}"
        )

    log.info("pages_deployed", project=project_name, stdout=result.stdout[-200:])
    return f"https://{project_name}.pages.dev"


def create_dns_record(slug: str, cf_token: str) -> str:
    """Create proxied A record demo-<slug>.clientcompass.co.za → 192.0.2.1.

    The demo-router Cloudflare Worker intercepts all *.clientcompass.co.za
    requests and proxies demo-<slug> → <slug>-demo.pages.dev.

    Returns the full demo URL.
    """
    record_name = f"demo-{slug}"
    demo_url = f"https://{record_name}.clientcompass.co.za"

    headers = {
        "Authorization": f"Bearer {cf_token}",
        "Content-Type": "application/json",
    }

    # Check if record already exists to avoid duplicates
    with httpx.Client(timeout=15) as client:
        resp = client.get(
            f"{_CF_API_BASE}/zones/{_CC_ZONE_ID}/dns_records",
            headers=headers,
            params={"name": f"{record_name}.clientcompass.co.za", "type": "A"},
        )
        resp.raise_for_status()
        existing = resp.json().get("result", [])

        if existing:
            log.info("dns_record_exists", name=record_name)
            return demo_url

        create_resp = client.post(
            f"{_CF_API_BASE}/zones/{_CC_ZONE_ID}/dns_records",
            headers=headers,
            json={
                "type": "A",
                "name": record_name,
                "content": "192.0.2.1",
                "proxied": True,
                "ttl": 1,
            },
        )
        create_resp.raise_for_status()
        result = create_resp.json()
        if not result.get("success"):
            raise RuntimeError(f"DNS create failed: {result.get('errors')}")

    log.info("dns_record_created", name=record_name, url=demo_url)
    return demo_url


def delete_pages_project(project_name: str, cf_token: str | None = None) -> bool:
    """Delete a Cloudflare Pages project by name. Returns True if successful."""
    if cf_token is None:
        cf_token = get_settings().cloudflare_api_token

    headers = {"Authorization": f"Bearer {cf_token}"}
    with httpx.Client(timeout=20) as client:
        resp = client.delete(
            f"{_CF_API_BASE}/accounts/{_CF_ACCOUNT_ID}/pages/projects/{project_name}",
            headers=headers,
        )
        if resp.status_code == 404:
            return True
        resp.raise_for_status()
        result = resp.json()
        ok = bool(result.get("success"))
        if ok:
            log.info("pages_project_deleted", project=project_name)
        return ok


def delete_dns_record(record_id: str, cf_token: str | None = None) -> bool:
    """Delete a DNS A record by its Cloudflare record ID."""
    if cf_token is None:
        cf_token = get_settings().cloudflare_api_token

    headers = {"Authorization": f"Bearer {cf_token}"}
    with httpx.Client(timeout=15) as client:
        resp = client.delete(
            f"{_CF_API_BASE}/zones/{_CC_ZONE_ID}/dns_records/{record_id}",
            headers=headers,
        )
        if resp.status_code == 404:
            return True
        resp.raise_for_status()
        return bool(resp.json().get("success"))


def list_pages_projects(cf_token: str | None = None) -> list[dict]:
    """List all Cloudflare Pages projects in the account.

    Cloudflare Pages API only accepts per_page=10 (hardcoded). We paginate
    with `page` param until result_info.total_pages is reached.
    """
    if cf_token is None:
        cf_token = get_settings().cloudflare_api_token

    headers = {"Authorization": f"Bearer {cf_token}"}
    projects: list[dict] = []
    with httpx.Client(timeout=30) as client:
        page = 1
        while True:
            resp = client.get(
                f"{_CF_API_BASE}/accounts/{_CF_ACCOUNT_ID}/pages/projects",
                headers=headers,
                params={"page": page, "per_page": 10},
            )
            resp.raise_for_status()
            result = resp.json()
            projects.extend(result.get("result", []))
            info = result.get("result_info", {})
            total_pages = info.get("total_pages", 1)
            current_page = info.get("page", 1)
            if current_page >= total_pages:
                break
            page += 1
    return projects


def list_demo_dns_records(cf_token: str | None = None) -> list[dict]:
    """List all demo-<slug>.clientcompass.co.za A records.

    The `name=demo-*.clientcompass.co.za` pattern is CF's wildcard match syntax.
    Per_page=100 is the DNS records API max.
    """
    if cf_token is None:
        cf_token = get_settings().cloudflare_api_token

    headers = {"Authorization": f"Bearer {cf_token}"}
    records: list[dict] = []
    with httpx.Client(timeout=30) as client:
        page = 1
        while True:
            resp = client.get(
                f"{_CF_API_BASE}/zones/{_CC_ZONE_ID}/dns_records",
                headers=headers,
                params={"type": "A", "name": "demo-*.clientcompass.co.za", "page": page, "per_page": 100},
            )
            resp.raise_for_status()
            result = resp.json()
            records.extend(result.get("result", []))
            info = result.get("result_info", {})
            if info.get("page", 1) >= info.get("total_pages", 1):
                break
            page += 1
    return records


def deploy_mockup(slug: str, dist_dir: str) -> str:
    """Full deploy: Pages + DNS. Returns the clientcompass.co.za demo URL."""
    settings = get_settings()
    cf_token = settings.cloudflare_api_token

    project_name = f"{slug}-demo"

    pages_url = deploy_pages(dist_dir=dist_dir, project_name=project_name, cf_token=cf_token)
    log.info("mockup_pages_live", slug=slug, pages_url=pages_url)

    demo_url = create_dns_record(slug=slug, cf_token=cf_token)
    log.info("mockup_dns_live", slug=slug, demo_url=demo_url)

    return demo_url
