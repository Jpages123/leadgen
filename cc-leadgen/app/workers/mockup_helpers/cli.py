"""CLI entry point — `python3 -m app.workers.mockup_helpers <subcommand> ...`"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from . import build as build_mod
from . import deploy as deploy_mod
from . import load_lead as lead_mod
from . import verify as verify_mod
from . import write_approval as approval_mod
from . import write_config as config_mod
from app.utils.mockup_build import mockup_project_dir


def _ok(data: dict) -> int:
    print(json.dumps({"ok": True, **data}, default=str))
    return 0


def _err(msg: str, **extra) -> int:
    print(json.dumps({"ok": False, "error": msg, **extra}, default=str))
    return 1


def cmd_load_lead(args: argparse.Namespace) -> int:
    try:
        data = lead_mod.load_lead(args.lead_id)
        return _ok({"lead": data})
    except Exception as e:
        return _err(f"load_lead failed: {e}", lead_id=args.lead_id)


def cmd_clone_template(args: argparse.Namespace) -> int:
    try:
        result = build_mod.clone_template(
            template=args.template,
            slug=args.slug,
            build_dir=args.build_dir,
        )
        return _ok(result)
    except Exception as e:
        return _err(f"clone_template failed: {e}", template=args.template, slug=args.slug)


def cmd_write_config(args: argparse.Namespace) -> int:
    try:
        result = config_mod.write_config(
            slug=args.slug,
            client_ts_file=Path(args.client_ts_file),
            brand_ts_file=Path(args.brand_ts_file),
        )
        return _ok(result)
    except Exception as e:
        return _err(f"write_config failed: {e}", slug=args.slug)


def cmd_copy_assets(args: argparse.Namespace) -> int:
    try:
        gallery = [p for p in (args.gallery or "").split(",") if p]
        result = build_mod.copy_assets(
            slug=args.slug,
            logo_path=args.logo,
            hero_path=args.hero,
            gallery_paths=gallery,
            business_name=args.business_name,
            accent_color=args.accent_color,
            build_dir=args.build_dir,
        )
        return _ok(result)
    except Exception as e:
        return _err(f"copy_assets failed: {e}", slug=args.slug)


def cmd_build(args: argparse.Namespace) -> int:
    try:
        result = build_mod.build(
            slug=args.slug,
            build_dir=args.build_dir,
            timeout_s=args.timeout,
        )
        return _ok(result)
    except Exception as e:
        return _err(f"build failed: {e}", slug=args.slug)


def cmd_deploy(args: argparse.Namespace) -> int:
    try:
        result = deploy_mod.deploy(
            slug=args.slug,
            build_dir=args.build_dir,
        )
        return _ok(result)
    except Exception as e:
        return _err(f"deploy failed: {e}", slug=args.slug)


def cmd_verify(args: argparse.Namespace) -> int:
    try:
        result = verify_mod.verify(
            url=args.url,
            vertical=args.vertical,
        )
        return _ok(result)
    except Exception as e:
        return _err(f"verify failed: {e}", url=args.url)


def cmd_write_approval(args: argparse.Namespace) -> int:
    try:
        rec = json.loads(args.recommendation_json) if args.recommendation_json else {}
        result = approval_mod.write_approval(
            lead_id=args.lead_id,
            mockup_url=args.mockup_url,
            recommendation=rec,
        )
        return _ok(result)
    except Exception as e:
        return _err(f"write_approval failed: {e}", lead_id=args.lead_id)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Mockup builder helper")
    sub = parser.add_subparsers(dest="cmd", required=True)

    # load_lead
    p = sub.add_parser("load_lead", help="Load lead context from DB")
    p.add_argument("lead_id")

    # clone_template
    p = sub.add_parser("clone_template", help="Clone a template repo")
    p.add_argument("template", choices=["trades", "creative", "general"])
    p.add_argument("slug")
    p.add_argument("--build-dir", default="")

    # write_config
    p = sub.add_parser("write_config", help="Write client.ts + brand.ts files")
    p.add_argument("slug")
    p.add_argument("--client-ts-file", required=True)
    p.add_argument("--brand-ts-file", required=True)
    p.add_argument("--build-dir", default="")

    # copy_assets
    p = sub.add_parser("copy_assets", help="Copy scraped images into project")
    p.add_argument("slug")
    p.add_argument("--logo", default=None)
    p.add_argument("--hero", default=None)
    p.add_argument("--gallery", default="", help="Comma-separated paths")
    p.add_argument("--business-name", default="Business")
    p.add_argument("--accent-color", default="#1a4d5c")
    p.add_argument("--build-dir", default="")

    # build
    p = sub.add_parser("build", help="pnpm install + pnpm build")
    p.add_argument("slug")
    p.add_argument("--build-dir", default="")
    p.add_argument("--timeout", type=int, default=180)

    # deploy
    p = sub.add_parser("deploy", help="wrangler deploy + DNS")
    p.add_argument("slug")
    p.add_argument("--build-dir", default="")

    # verify
    p = sub.add_parser("verify", help="Programmatic mockup verification")
    p.add_argument("url")
    p.add_argument("vertical", help="Business vertical (event_planner, plumber, etc.)")

    # write_approval
    p = sub.add_parser("write_approval", help="Write approval row to prod DB")
    p.add_argument("lead_id")
    p.add_argument("mockup_url")
    p.add_argument("recommendation_json", help="JSON string of the Pi recommendation")

    args = parser.parse_args(argv)
    handlers = {
        "load_lead": cmd_load_lead,
        "clone_template": cmd_clone_template,
        "write_config": cmd_write_config,
        "copy_assets": cmd_copy_assets,
        "build": cmd_build,
        "deploy": cmd_deploy,
        "verify": cmd_verify,
        "write_approval": cmd_write_approval,
    }
    return handlers[args.cmd](args)


if __name__ == "__main__":
    sys.exit(main())
