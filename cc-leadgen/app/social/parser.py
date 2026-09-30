"""Parser for vault social-media post files.

Files live in the Obsidian vault Posts dir (Syncthing-synced to the laptop,
mounted read-only at ``settings.social_posts_dir``) and are named
``social-media-post-<N>-<slug>.md``. The generator prompt that defines the
format lives in toolkit/bin/generate_posts.py on the VPS.

Format (post 77 onwards):

    # Social Media Post <N> — <D Month YYYY> (<Weekday>)

    **Platform:** Facebook & Instagram
    **Format:** Single image post
    **Suggested posting time:** <freeform one-liner>
    **Schedule (SAST):** YYYY-MM-DD HH:MM          # optional

    ## Angle: <short title>

    ### Caption (primary)      → caption_fb
    ### Caption (alt — short)  → caption_ig
    ### Image prompt
    ### Stock photo search     → bullet lines → search_queries
    ### First comment          → incl. CTA URL

Tolerant of em-dash/en-dash/hyphen variants in headings and of trailing
whitespace. Returns None (with a warning) when either caption is missing —
the caller skips the file without inserting.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

from app.utils.logger import get_logger

log = get_logger(__name__)

SAST = timezone(timedelta(hours=2))

POST_FILE_RE = re.compile(r"^social-media-post-(\d+)-.+\.md$")

_HEADING_RE = re.compile(r"^#{1,6}\s+(.*?)\s*$")
_ANGLE_HEADING_RE = re.compile(r"^angle\s*:\s*(.+)$", re.IGNORECASE)
_SUGGESTED_RE = re.compile(r"^\*\*\s*Suggested posting time\s*:\*\*\s*(.+?)\s*$", re.IGNORECASE)
_SCHEDULE_RE = re.compile(r"^\*\*\s*Schedule\s*\(SAST\)\s*:\*\*\s*(.+?)\s*$", re.IGNORECASE)
_SCHEDULE_DT_RE = re.compile(r"(\d{4})-(\d{2})-(\d{2})\s+(\d{1,2}):(\d{2})")
_BULLET_RE = re.compile(r"^[-*•]\s+(.+?)\s*$")
_DASHES_RE = re.compile(r"[—–−]")


def _norm_heading(text: str) -> str:
    """Normalise a heading for matching: unicode dashes → '-', collapse space, lower."""
    return re.sub(r"\s+", " ", _DASHES_RE.sub("-", text)).strip().lower()


def _parse_schedule(raw: str | None) -> datetime | None:
    """'2026-10-01 20:00' (anywhere in the value) → tz-aware SAST datetime."""
    if not raw:
        return None
    m = _SCHEDULE_DT_RE.search(raw)
    if not m:
        return None
    try:
        return datetime(
            int(m.group(1)), int(m.group(2)), int(m.group(3)),
            int(m.group(4)), int(m.group(5)), tzinfo=SAST,
        )
    except ValueError:
        return None


@dataclass
class ParsedPost:
    source_file: str
    post_number: int | None
    angle: str | None
    caption_fb: str
    caption_ig: str
    first_comment: str | None = None
    image_prompt: str | None = None
    suggested_time_note: str | None = None
    scheduled_at: datetime | None = None
    search_queries: list[str] = field(default_factory=list)


def parse_post_text(text: str, source_file: str) -> ParsedPost | None:
    """Parse one post file's contents. None → caller skips (no insert)."""
    m = POST_FILE_RE.match(source_file)
    post_number = int(m.group(1)) if m else None

    angle = None
    suggested_time_note = None
    schedule_raw = None
    sections: list[tuple[str, str]] = []  # (normalized heading, body)
    cur_heading: str | None = None
    cur_lines: list[str] = []

    def _flush() -> None:
        nonlocal cur_heading, cur_lines
        if cur_heading is not None:
            sections.append((cur_heading, "\n".join(cur_lines).strip()))
        cur_lines = []

    for raw_line in text.splitlines():
        line = raw_line.rstrip()
        hm = _HEADING_RE.match(line)
        if hm:
            _flush()
            heading_text = hm.group(1)
            am = _ANGLE_HEADING_RE.match(heading_text)
            if am:
                angle = am.group(1).strip()
            cur_heading = _norm_heading(heading_text)
            continue
        sm = _SUGGESTED_RE.match(line.strip())
        if sm and suggested_time_note is None:
            suggested_time_note = sm.group(1).strip()
        sch = _SCHEDULE_RE.match(line.strip())
        if sch and schedule_raw is None:
            schedule_raw = sch.group(1).strip()
        if cur_heading is not None:
            cur_lines.append(line)
    _flush()

    def _section(prefix: str) -> str | None:
        for norm, body in sections:
            if norm.startswith(prefix):
                return body or None
        return None

    caption_fb = _section("caption (primary")
    caption_ig = _section("caption (alt")

    if not caption_fb or not caption_ig:
        log.warning(
            "social_post_missing_caption",
            file=source_file,
            has_primary=bool(caption_fb),
            has_alt=bool(caption_ig),
        )
        return None

    queries: list[str] = []
    stock_body = _section("stock photo search")
    if stock_body:
        for line in stock_body.splitlines():
            bm = _BULLET_RE.match(line.strip())
            if bm:
                queries.append(bm.group(1).strip())
    if not queries and angle:
        queries = [angle]

    return ParsedPost(
        source_file=source_file,
        post_number=post_number,
        angle=angle,
        caption_fb=caption_fb,
        caption_ig=caption_ig,
        first_comment=_section("first comment"),
        image_prompt=_section("image prompt"),
        suggested_time_note=suggested_time_note,
        scheduled_at=_parse_schedule(schedule_raw),
        search_queries=queries,
    )


def parse_post_file(path: Path | str) -> ParsedPost | None:
    p = Path(path)
    try:
        text = p.read_text(encoding="utf-8")
    except OSError as exc:
        log.warning("social_post_read_failed", file=str(p), error=str(exc)[:200])
        return None
    return parse_post_text(text, p.name)
