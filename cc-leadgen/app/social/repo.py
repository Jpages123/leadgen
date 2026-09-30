"""Thin prod-DB layer for admin_crm.social_posts.

Connection mechanism mirrors app.workers.email_draft._get_prod_conn:
PROD_DB_URL + psycopg2 + SET LOCAL app.is_admin='true' per transaction
(admin_crm is RLS-gated on app.is_admin).

All DB access for the social pipeline goes through SocialPostRepo so the
tasks are testable with in-memory fakes — pass conn_factory or swap the
repo object entirely.
"""
from __future__ import annotations

import json
from contextlib import contextmanager
from datetime import timedelta
from urllib.parse import unquote, urlparse

import psycopg2
import psycopg2.extras

from app.config import get_settings


def _parse_prod_db_url(raw: str) -> dict:
    """Parse PROD_DB_URL safely (handles @ in password)."""
    cleaned = raw.replace("+asyncpg", "")
    parsed = urlparse(cleaned)
    return {
        "user": unquote(parsed.username or ""),
        "password": unquote(parsed.password or ""),
        "host": parsed.hostname,
        "port": parsed.port or 5432,
        "database": (parsed.path or "/").lstrip("/"),
    }


def connect():
    """Return a psycopg2 connection to the prod DB."""
    prod_url = get_settings().prod_db_url
    if not prod_url:
        raise RuntimeError("PROD_DB_URL not configured")
    cfg = _parse_prod_db_url(prod_url)
    return psycopg2.connect(
        user=cfg["user"], password=cfg["password"],
        host=cfg["host"], port=cfg["port"], dbname=cfg["database"],
        connect_timeout=10,
    )


class SocialPostRepo:
    """All prod-DB access for admin_crm.social_posts."""

    def __init__(self, conn_factory=None):
        self._conn_factory = conn_factory or connect

    @contextmanager
    def _tx(self):
        conn = self._conn_factory()
        try:
            with conn:
                with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                    cur.execute("SET LOCAL app.is_admin = 'true'")
                    yield cur
        finally:
            conn.close()

    # ── Ingest ────────────────────────────────────────────────────────

    def insert_post(self, post) -> str | None:
        """INSERT … ON CONFLICT (source_file) DO NOTHING → new id or None."""
        with self._tx() as cur:
            cur.execute(
                """
                INSERT INTO admin_crm.social_posts (
                    source_file, post_number, angle,
                    caption_fb, caption_ig, first_comment, image_prompt,
                    suggested_time_note, scheduled_at, search_queries
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (source_file) DO NOTHING
                RETURNING id
                """,
                (
                    post.source_file, post.post_number, post.angle,
                    post.caption_fb, post.caption_ig, post.first_comment,
                    post.image_prompt, post.suggested_time_note,
                    post.scheduled_at, list(post.search_queries),
                ),
            )
            row = cur.fetchone()
            return str(row["id"]) if row else None

    def save_candidates(self, post_id: str, candidates: list[dict],
                        queries: list[str] | None = None) -> None:
        """Store Pexels candidates + the queries that produced them (ingest)."""
        with self._tx() as cur:
            cur.execute(
                """
                UPDATE admin_crm.social_posts
                SET photo_candidates = %s::jsonb,
                    search_queries = %s,
                    updated_at = NOW()
                WHERE id = %s
                """,
                (json.dumps(candidates), list(queries or []), post_id),
            )

    def replace_candidates(self, post_id: str, query: str,
                           candidates: list[dict]) -> bool:
        """Operator re-search: replace candidates, append query. Editable rows only."""
        with self._tx() as cur:
            cur.execute(
                """
                UPDATE admin_crm.social_posts
                SET photo_candidates = %s::jsonb,
                    search_queries = array_append(search_queries, %s),
                    updated_at = NOW()
                WHERE id = %s AND status IN ('pending_review', 'approved')
                RETURNING id
                """,
                (json.dumps(candidates), query, post_id),
            )
            return cur.fetchone() is not None

    # ── Publish ───────────────────────────────────────────────────────

    def fail_stuck_publishing(self, stuck_after: timedelta) -> list[dict]:
        """Rows claimed but never finished → failed + alert-worthy error."""
        with self._tx() as cur:
            cur.execute(
                """
                UPDATE admin_crm.social_posts
                SET status = 'failed',
                    last_error = 'stuck in publishing — verify on FB/IG before retrying',
                    updated_at = NOW()
                WHERE status = 'publishing' AND updated_at < NOW() - %s
                RETURNING id, angle, source_file, last_error
                """,
                (stuck_after,),
            )
            return [dict(r) for r in cur.fetchall()]

    def fail_missed_window(self, window: timedelta) -> list[dict]:
        """Approved posts whose slot passed >window ago → failed (never published)."""
        with self._tx() as cur:
            cur.execute(
                """
                UPDATE admin_crm.social_posts
                SET status = 'failed',
                    last_error = 'missed publish window',
                    updated_at = NOW()
                WHERE status = 'approved'
                  AND scheduled_at IS NOT NULL
                  AND scheduled_at < NOW() - %s
                RETURNING id, angle, source_file
                """,
                (window,),
            )
            return [dict(r) for r in cur.fetchall()]

    def due_posts(self) -> list[dict]:
        """Approved posts due now with a chosen image."""
        with self._tx() as cur:
            cur.execute(
                """
                SELECT * FROM admin_crm.social_posts
                WHERE status = 'approved'
                  AND scheduled_at IS NOT NULL AND scheduled_at <= NOW()
                  AND image_data IS NOT NULL
                ORDER BY scheduled_at ASC
                """
            )
            return [dict(r) for r in cur.fetchall()]

    def claim(self, post_id: str) -> dict | None:
        """Atomic approved→publishing claim. None → another worker beat us to it."""
        with self._tx() as cur:
            cur.execute(
                """
                UPDATE admin_crm.social_posts
                SET status = 'publishing',
                    publish_attempts = publish_attempts + 1,
                    updated_at = NOW()
                WHERE id = %s AND status = 'approved'
                RETURNING *
                """,
                (post_id,),
            )
            row = cur.fetchone()
            return dict(row) if row else None

    def update_fields(self, post_id: str, **fields) -> None:
        if not fields:
            return
        assignments = ", ".join(f"{k} = %s" for k in fields)
        values = [
            json.dumps(v) if isinstance(v, (dict, list)) else v
            for v in fields.values()
        ]
        with self._tx() as cur:
            cur.execute(
                f"UPDATE admin_crm.social_posts SET {assignments}, updated_at = NOW() WHERE id = %s",
                (*values, post_id),
            )

    def mark_published(self, post_id: str) -> None:
        with self._tx() as cur:
            cur.execute(
                """
                UPDATE admin_crm.social_posts
                SET status = 'published',
                    published_at = NOW(),
                    last_error = NULL,
                    updated_at = NOW()
                WHERE id = %s
                """,
                (post_id,),
            )

    def mark_failed(self, post_id: str, error: str) -> None:
        with self._tx() as cur:
            cur.execute(
                """
                UPDATE admin_crm.social_posts
                SET status = 'failed',
                    last_error = %s,
                    updated_at = NOW()
                WHERE id = %s
                """,
                (error, post_id),
            )
