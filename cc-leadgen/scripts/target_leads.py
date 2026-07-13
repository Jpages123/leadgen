"""Flag/unflag leads as 'mockup_targeted' for the SELECT page.

Usage (run inside the worker container):

    # Show currently-targeted leads
    .venv/bin/python -m scripts.target_leads list

    # Flag 29 leads by ID (comma-separated) — will appear on SELECT page
    .venv/bin/python -m scripts.target_leads flag eb3ec99f-...,b5790be1-...

    # Flag by business name substring (case-insensitive)
    .venv/bin/python -m scripts.target_leads flag --name limelight
    .venv/bin/python -m scripts.target_leads flag --name "limelight|katia|blackjack"

    # Flag by city / platform / score range
    .venv/bin/python -m scripts.target_leads flag --city "cape town|durban" --score-min 85

    # Unflag
    .venv/bin/python -m scripts.target_leads unflag eb3ec99f-...

    # Clear ALL targeted flags (reset)
    .venv/bin/python -m scripts.target_leads clear
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, "/app")

from app.db.sync_session import sync_session_scope  # noqa: E402
from app.models.lead import Lead  # noqa: E402
from sqlalchemy import select, update  # noqa: E402


def _jsonable(v):
    if v is None or isinstance(v, (str, int, float, bool)):
        return v
    return str(v)


def cmd_list(args):
    with sync_session_scope() as s:
        stmt = (
            select(
                Lead.id, Lead.business_name, Lead.business_type, Lead.city,
                Lead.web_pitch_score, Lead.website_platform, Lead.mockup_status,
                Lead.phone, Lead.email,
            )
            .where(Lead.mockup_targeted.is_(True))
            .order_by(Lead.web_pitch_score.desc(), Lead.business_name)
        )
        rows = s.execute(stmt).all()
        print(f"=== {len(rows)} targeted leads ===")
        if not rows:
            print("  (none — use `flag` to add)")
            return
        print(f"{'#':<3} {'Score':<6} {'Status':<18} {'Platform':<18} {'Business':<35} {'City':<15} {'Contact'}")
        print("-" * 130)
        for i, r in enumerate(rows, 1):
            contact = []
            if r.phone: contact.append(f"ph:{r.phone[:14]}")
            if r.email: contact.append(f"em:{r.email[:20]}")
            print(
                f"{i:<3} {r.web_pitch_score or 0:<6} "
                f"{r.mockup_status or 'none':<18} "
                f"{(r.website_platform or 'unknown')[:18]:<18} "
                f"{(r.business_name or '—')[:35]:<35} "
                f"{(r.city or '—')[:15]:<15} "
                f"{', '.join(contact) or 'NONE'}"
            )


def _resolve_ids(args) -> list[str] | None:
    """Build the list of lead IDs to flag from various selectors.

    Returns None if no selectors provided.
    """
    ids: list[str] = []
    if args.ids:
        ids.extend(i.strip() for i in args.ids.split(",") if i.strip())
    return ids or None


def _find_by_filters(args) -> list[str]:
    """Find lead IDs by name/city/platform/score filter."""
    from sqlalchemy import or_, and_
    with sync_session_scope() as s:
        stmt = select(Lead.id)
        conditions = []
        if args.name:
            # Pipe-separated list of substrings (case-insensitive)
            substrings = [n.strip().lower() for n in args.name.split("|") if n.strip()]
            name_conds = [
                Lead.business_name.ilike(f"%{n}%") for n in substrings
            ]
            conditions.append(or_(*name_conds))
        if args.city:
            cities = [c.strip().lower() for c in args.city.split("|") if c.strip()]
            city_conds = [Lead.city.ilike(f"%{c}%") for c in cities]
            conditions.append(or_(*city_conds))
        if args.platform:
            platforms = [p.strip() for p in args.platform.split("|") if p.strip()]
            conditions.append(Lead.website_platform.in_(platforms))
        if args.score_min is not None:
            conditions.append(Lead.web_pitch_score >= args.score_min)
        if args.status:
            conditions.append(Lead.mockup_status == args.status)

        if not conditions:
            return []
        ids = [str(r[0]) for r in s.execute(stmt.where(and_(*conditions))).all()]
        return ids


def cmd_flag(args):
    explicit_ids = _resolve_ids(args) or []
    filter_ids = _find_by_filters(args) or []

    all_ids = list(dict.fromkeys(explicit_ids + filter_ids))  # dedupe, preserve order
    if not all_ids:
        print("ERROR: no leads matched. Provide --ids or filter args (--name, --city, etc).", file=sys.stderr)
        return 1

    if args.dry_run:
        print(f"[DRY RUN] Would flag {len(all_ids)} leads:")
        for lid in all_ids:
            print(f"  {lid}")
        return 0

    with sync_session_scope() as s:
        stmt = (
            update(Lead)
            .where(Lead.id.in_(all_ids))
            .values(mockup_targeted=True)
        )
        result = s.execute(stmt)
        print(f"Flagged {result.rowcount} of {len(all_ids)} leads as mockup_targeted.")
        if result.rowcount < len(all_ids):
            print(f"  (note: {len(all_ids) - result.rowcount} IDs didn't exist in the DB)")
    return 0


def cmd_unflag(args):
    explicit_ids = _resolve_ids(args) or []
    filter_ids = _find_by_filters(args) or []
    all_ids = list(dict.fromkeys(explicit_ids + filter_ids))

    if not all_ids:
        print("ERROR: no leads matched. Provide --ids or filter args.", file=sys.stderr)
        return 1

    if args.dry_run:
        print(f"[DRY RUN] Would unflag {len(all_ids)} leads:")
        for lid in all_ids:
            print(f"  {lid}")
        return 0

    with sync_session_scope() as s:
        stmt = (
            update(Lead)
            .where(Lead.id.in_(all_ids))
            .values(mockup_targeted=False)
        )
        result = s.execute(stmt)
        print(f"Unflagged {result.rowcount} of {len(all_ids)} leads.")
    return 0


def cmd_clear(args):
    if args.dry_run:
        with sync_session_scope() as s:
            n = s.execute(select(Lead).where(Lead.mockup_targeted.is_(True))).all()
            print(f"[DRY RUN] Would clear mockup_targeted flag on {len(n)} leads.")
        return 0
    with sync_session_scope() as s:
        stmt = update(Lead).values(mockup_targeted=False)
        result = s.execute(stmt)
        print(f"Cleared mockup_targeted on {result.rowcount} leads.")
    return 0


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="cmd", required=True)

    # list
    p = sub.add_parser("list", help="List all targeted leads")
    p.set_defaults(func=cmd_list)

    # flag
    p = sub.add_parser("flag", help="Flag leads as targeted")
    p.add_argument("--ids", help="Comma-separated lead UUIDs")
    p.add_argument("--name", help="Pipe-separated business name substrings (case-insensitive)")
    p.add_argument("--city", help="Pipe-separated city substrings")
    p.add_argument("--platform", help="Pipe-separated website_platform values")
    p.add_argument("--score-min", type=int, help="Minimum web_pitch_score")
    p.add_argument("--status", help="Filter by current mockup_status (e.g. 'none', 'pending_approval')")
    p.add_argument("--dry-run", action="store_true")
    p.set_defaults(func=cmd_flag)

    # unflag
    p = sub.add_parser("unflag", help="Unflag leads")
    p.add_argument("--ids", help="Comma-separated lead UUIDs")
    p.add_argument("--name", help="Pipe-separated business name substrings")
    p.add_argument("--city", help="Pipe-separated city substrings")
    p.add_argument("--platform", help="Pipe-separated website_platform values")
    p.add_argument("--score-min", type=int)
    p.add_argument("--status")
    p.add_argument("--dry-run", action="store_true")
    p.set_defaults(func=cmd_unflag)

    # clear
    p = sub.add_parser("clear", help="Clear all mockup_targeted flags")
    p.add_argument("--dry-run", action="store_true")
    p.set_defaults(func=cmd_clear)

    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main() or 0)
