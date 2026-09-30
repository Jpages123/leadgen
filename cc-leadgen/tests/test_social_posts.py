"""Tests for the social-post pipeline (parser + publisher tasks).

No prod DB, no network — repo is an in-memory fake, httpx is a FakeClient.

Run with:
    cd ~/installedApps/leadgen/cc-leadgen && source .venv/bin/activate \\
        && pytest -c /dev/null tests/test_social_posts.py -v \\
            --no-header -p no:cacheprovider
"""
from __future__ import annotations

import os
import sys
import uuid
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest

from app.social import parser
import app.workers.social as social_worker


# ── Post-file fixtures (post 77 verbatim + new sections) ──────────────────────

POST77_BODY = """\
# Social Media Post 77 — 30 September 2026 (Wednesday)

**Platform:** Facebook & Instagram
**Format:** Single image post
**Suggested posting time:** Thursday 1 Oct, 20:00–21:00 SAST — post it *during* the second shift itself.

## Angle: The Second Shift — "You finished work at 5. Then the real admin started."

### Caption (primary)

There's a shift nobody pays you for.

Tools down at 5. Bakkie parked. Supper. Then the laptop opens — the quote you promised.

R200/month, first month free → clientcompass.co.za

### Caption (alt — short)

Work ends at 5. Admin starts at 8.

Ten minutes, not two hours. R200/month, first month free → clientcompass.co.za

### Image prompt

Flat-design illustration, evening scene: a person on a couch at night with a laptop glowing.

### First comment

How many hours a week does your second shift take? First month free:
https://clientcompass.co.za/?utm_source=social&utm_medium=organic&utm_campaign=post77
"""

POST77_FULL = POST77_BODY.replace(
    "**Suggested posting time:** Thursday 1 Oct, 20:00–21:00 SAST — post it *during* the second shift itself.\n",
    "**Suggested posting time:** Thursday 1 Oct, 20:00–21:00 SAST — post it *during* the second shift itself.\n"
    "**Schedule (SAST):** 2026-10-01 20:00\n",
).replace(
    "### First comment\n",
    "### Stock photo search\n\n- tradesman laptop evening\n- small business owner night\n\n### First comment\n",
)


def _parse(text, name="social-media-post-77-second-shift.md"):
    return parser.parse_post_text(text, name)


# ── Parser tests ─────────────────────────────────────────────────────────────

def test_parser_full_fixture():
    p = _parse(POST77_FULL)
    assert p is not None
    assert p.post_number == 77
    assert p.angle == 'The Second Shift — "You finished work at 5. Then the real admin started."'
    assert p.caption_fb.startswith("There's a shift nobody pays you for.")
    assert "clientcompass.co.za" in p.caption_fb
    assert p.caption_ig.startswith("Work ends at 5. Admin starts at 8.")
    assert p.first_comment.startswith("How many hours")
    assert "utm_campaign=post77" in p.first_comment
    assert p.image_prompt.startswith("Flat-design illustration")
    assert p.suggested_time_note.startswith("Thursday 1 Oct")
    assert p.scheduled_at == datetime(2026, 10, 1, 20, 0, tzinfo=parser.SAST)


def test_parser_missing_new_sections_tolerated():
    p = _parse(POST77_BODY)
    assert p is not None
    assert p.scheduled_at is None


def test_parser_hyphen_alt_caption_variant():
    p = _parse(POST77_BODY.replace("### Caption (alt — short)", "### Caption (alt - short)"))
    assert p is not None
    assert p.caption_ig.startswith("Work ends at 5.")


def test_parser_endash_alt_caption_variant():
    p = _parse(POST77_BODY.replace("### Caption (alt — short)", "### Caption (alt – short)"))
    assert p is not None
    assert p.caption_ig.startswith("Work ends at 5.")


def test_parser_trailing_whitespace():
    messy = "\n".join(line + "   " for line in POST77_FULL.splitlines())
    p = _parse(messy)
    assert p is not None
    assert p.scheduled_at == datetime(2026, 10, 1, 20, 0, tzinfo=parser.SAST)


def test_parser_unparseable_schedule_is_none():
    text = POST77_FULL.replace("**Schedule (SAST):** 2026-10-01 20:00",
                               "**Schedule (SAST):** <YYYY-MM-DD HH:MM — the exact slot>")
    p = _parse(text)
    assert p is not None
    assert p.scheduled_at is None


def test_parser_missing_caption_skips():
    no_alt = POST77_BODY.split("### Caption (alt")[0] + "### Image prompt\n\nx\n"
    assert _parse(no_alt) is None
    no_primary = POST77_BODY.replace("### Caption (primary)", "### Notes")
    assert _parse(no_primary) is None


# ── Fakes ─────────────────────────────────────────────────────────────────────

def _now():
    return datetime.now(timezone.utc)


def make_row(**over) -> dict:
    row = {
        "id": str(uuid.uuid4()),
        "source_file": "social-media-post-77-second-shift.md",
        "post_number": 77, "angle": "Second Shift",
        "caption_fb": "fb caption", "caption_ig": "ig caption",
        "first_comment": "comment text https://example.com",
        "image_prompt": None, "suggested_time_note": None,
        "scheduled_at": _now() - timedelta(minutes=1),
        "search_queries": [], "photo_candidates": [],
        "image_data": b"\xff\xd8fake-jpeg", "image_source": None,
        "status": "approved",
        "fb_post_id": None, "fb_comment_id": None,
        "ig_container_id": None, "ig_media_id": None, "ig_comment_id": None,
        "ig_permalink": None, "last_error": None, "comment_error": None,
        "publish_attempts": 0, "approved_at": _now(), "published_at": None,
        "created_at": _now(), "updated_at": _now(),
    }
    row.update(over)
    return row


class FakeRepo:
    """In-memory stand-in for SocialPostRepo mirroring the SQL semantics."""

    def __init__(self, rows=()):
        self.rows = {str(r["id"]): dict(r) for r in rows}

    def get(self, post_id):
        return self.rows[str(post_id)]

    def insert_post(self, post):
        if any(r["source_file"] == post.source_file for r in self.rows.values()):
            return None
        row = make_row(
            source_file=post.source_file, post_number=post.post_number,
            angle=post.angle, caption_fb=post.caption_fb, caption_ig=post.caption_ig,
            first_comment=post.first_comment, image_prompt=post.image_prompt,
            suggested_time_note=post.suggested_time_note,
            scheduled_at=post.scheduled_at,
            image_data=None, status="pending_review", approved_at=None,
        )
        self.rows[row["id"]] = row
        return row["id"]

    def fail_stuck_publishing(self, stuck_after):
        out = []
        for r in self.rows.values():
            if r["status"] == "publishing" and r["updated_at"] < _now() - stuck_after:
                r["status"] = "failed"
                r["last_error"] = "stuck in publishing — verify on FB/IG before retrying"
                r["updated_at"] = _now()
                out.append(dict(r))
        return out

    def fail_missed_window(self, window):
        out = []
        for r in self.rows.values():
            if (r["status"] == "approved" and r["scheduled_at"]
                    and r["scheduled_at"] < _now() - window):
                r["status"] = "failed"
                r["last_error"] = "missed publish window"
                out.append(dict(r))
        return out

    def due_posts(self):
        return [dict(r) for r in self.rows.values()
                if r["status"] == "approved" and r["scheduled_at"]
                and r["scheduled_at"] <= _now() and r["image_data"]]

    def claim(self, post_id):
        r = self.rows.get(str(post_id))
        if not r or r["status"] != "approved":
            return None
        r["status"] = "publishing"
        r["publish_attempts"] += 1
        r["updated_at"] = _now()
        return r

    def update_fields(self, post_id, **fields):
        self.rows[str(post_id)].update(fields)

    def mark_published(self, post_id):
        r = self.get(post_id)
        r["status"] = "published"
        r["published_at"] = _now()
        r["last_error"] = None

    def mark_failed(self, post_id, error):
        r = self.get(post_id)
        r["status"] = "failed"
        r["last_error"] = error


class FakeResp:
    def __init__(self, data=None, status=200, text=""):
        self._data = data or {}
        self.status_code = status
        self.text = text or str(self._data)

    def json(self):
        return self._data


class FakeClient:
    """httpx.Client stand-in routing Graph calls by URL path."""

    def __init__(self, fail_paths=None):
        self.calls = []
        self.fail_paths = fail_paths or {}
        self.closed = False

    def post(self, url, data=None, timeout=None):
        self.calls.append(("POST", url))
        return self._handle("POST", url)

    def get(self, url, params=None, timeout=None):
        self.calls.append(("GET", url))
        return self._handle("GET", url)

    def _handle(self, method, url):
        for substr, resp in self.fail_paths.items():
            if substr in url:
                if isinstance(resp, Exception):
                    raise resp
                return resp
        if method == "POST" and url.endswith("/photos"):
            return FakeResp({"id": "fb_post_1", "post_id": "fb_post_1"})
        if method == "POST" and url.endswith("/media_publish"):
            return FakeResp({"id": "ig_media_1"})
        if method == "POST" and url.endswith("/media"):
            return FakeResp({"id": "ig_container_1"})
        if method == "POST" and url.endswith("/comments"):
            return FakeResp({"id": "comment_1"})
        if method == "GET" and "ig_container_1" in url:
            return FakeResp({"status_code": "FINISHED"})
        if method == "GET" and "ig_media_1" in url:
            return FakeResp({"permalink": "https://www.instagram.com/p/abc123/"})
        return FakeResp({})

    def close(self):
        self.closed = True


def _settings(**over):
    base = dict(
        meta_graph_version="v22.0",
        meta_page_id="page_1",
        meta_ig_user_id="ig_user_1",
        meta_page_access_token="secret-token",
        social_image_base_url="https://login.example.test/social-media",
        social_missed_window_hours=6,
        social_ingest_max_age_days=10,
        social_posts_dir="/vault/posts",
        smtp_host="", smtp_port=587, smtp_user="", smtp_pass="",
        smtp_from_email="", smtp_from_name="Client Compass",
        ops_alert_email="info@clientcompass.co.za",
    )
    base.update(over)
    return SimpleNamespace(**base)


@pytest.fixture(autouse=True)
def _patch_settings(monkeypatch):
    monkeypatch.setattr(social_worker, "get_settings", lambda: _settings())


@pytest.fixture(autouse=True)
def _no_alert_email(monkeypatch):
    sent = []
    monkeypatch.setattr(social_worker, "_send_alert_email",
                        lambda subject, body, **kw: sent.append((subject, body)) or True)
    return sent


NO_SLEEP = lambda s: None


# ── Publisher tests ───────────────────────────────────────────────────────────

def test_publish_not_configured_is_noop(monkeypatch):
    monkeypatch.setattr(social_worker, "get_settings",
                        lambda: _settings(meta_page_id="", meta_ig_user_id="",
                                          meta_page_access_token=""))
    stuck = make_row(status="publishing", updated_at=_now() - timedelta(hours=2))
    repo = FakeRepo([stuck])
    client = FakeClient()
    result = social_worker.publish_due_social_posts(repo=repo, client=client)
    assert result["reason"] == "not_configured"
    # zero state changes — even the stuck guard must not run
    assert repo.get(stuck["id"])["status"] == "publishing"
    assert client.calls == []


def test_publish_happy_path(_no_alert_email):
    repo = FakeRepo([make_row()])
    client = FakeClient()
    result = social_worker.publish_due_social_posts(repo=repo, client=client,
                                                    sleep=NO_SLEEP)
    assert result["published"] == 1 and result["failed"] == 0
    row = repo.rows[list(repo.rows)[0]]
    assert row["status"] == "published"
    assert row["fb_post_id"] == "fb_post_1"
    assert row["ig_container_id"] == "ig_container_1"
    assert row["ig_media_id"] == "ig_media_1"
    assert row["ig_permalink"] == "https://www.instagram.com/p/abc123/"
    assert row["fb_comment_id"] == "comment_1"
    assert row["ig_comment_id"] == "comment_1"
    assert row["publish_attempts"] == 1
    paths = [u for m, u in client.calls]
    assert any(p.endswith("/page_1/photos") for p in paths)
    assert any(p.endswith("/ig_user_1/media") for p in paths)
    assert any(p.endswith("/ig_user_1/media_publish") for p in paths)
    assert any(p.endswith("/fb_post_1/comments") for p in paths)
    assert any(p.endswith("/ig_media_1/comments") for p in paths)
    assert _no_alert_email == []


def test_publish_missed_window_fails_without_graph(_no_alert_email):
    stale = make_row(scheduled_at=_now() - timedelta(hours=7))
    repo = FakeRepo([stale])
    client = FakeClient()
    result = social_worker.publish_due_social_posts(repo=repo, client=client,
                                                    sleep=NO_SLEEP)
    assert result["published"] == 0
    row = repo.get(stale["id"])
    assert row["status"] == "failed"
    assert row["last_error"] == "missed publish window"
    assert client.calls == []            # never published
    assert len(_no_alert_email) == 1     # ops alerted


def test_publish_ig_failure_keeps_fb_id_then_resumes(_no_alert_email):
    repo = FakeRepo([make_row()])
    bad_ig = FakeClient(fail_paths={
        "/media": FakeResp({"error": {"message": "IG exploded"}}, status=500),
    })
    r1 = social_worker.publish_due_social_posts(repo=repo, client=bad_ig,
                                                sleep=NO_SLEEP)
    assert r1["failed"] == 1
    row = repo.rows[list(repo.rows)[0]]
    assert row["status"] == "failed"
    assert row["fb_post_id"] == "fb_post_1"     # persisted before the IG failure
    assert row["ig_container_id"] is None
    assert "IG exploded" in row["last_error"]
    assert "secret-token" not in row["last_error"]

    # Operator retries → only the IG leg runs; FB is not re-posted.
    row["status"] = "approved"
    row["scheduled_at"] = _now() - timedelta(minutes=1)
    good = FakeClient()
    r2 = social_worker.publish_due_social_posts(repo=repo, client=good,
                                                sleep=NO_SLEEP)
    assert r2["published"] == 1
    assert not any(u.endswith("/photos") for _, u in good.calls)
    assert row["status"] == "published"
    assert row["ig_media_id"] == "ig_media_1"


@pytest.mark.parametrize("stale", [
    FakeResp({"status_code": "ERROR"}),                    # container died
    FakeResp({"status_code": "EXPIRED"}),                  # >24h old
    social_worker.meta.MetaError("container gone"),        # lookup failed
])
def test_publish_stale_ig_container_recreates(_no_alert_email, stale):
    """Retry where the stored ig_container_id is dead: a fresh container is
    created and used; the already-posted FB leg is not repeated."""
    repo = FakeRepo([make_row(
        fb_post_id="fb_post_1",
        ig_container_id="ig_container_stale",
    )])
    client = FakeClient(fail_paths={"ig_container_stale": stale})
    result = social_worker.publish_due_social_posts(repo=repo, client=client,
                                                    sleep=NO_SLEEP)
    assert result["published"] == 1
    row = repo.rows[list(repo.rows)[0]]
    assert row["status"] == "published"
    assert row["ig_container_id"] == "ig_container_1"      # fresh, not the stale one
    assert row["ig_media_id"] == "ig_media_1"
    posts = [u for m, u in client.calls if m == "POST"]
    assert not any(u.endswith("/photos") for u in posts)   # FB not re-posted
    assert sum(1 for u in posts if u.endswith("/media")) == 1  # one new container


def test_publish_claim_returns_none_no_graph_calls():
    repo = FakeRepo([make_row()])
    repo.claim = lambda post_id: None
    client = FakeClient()
    result = social_worker.publish_due_social_posts(repo=repo, client=client,
                                                    sleep=NO_SLEEP)
    assert result["skipped"] == 1
    assert client.calls == []


def test_publish_comment_failure_still_publishes(_no_alert_email):
    repo = FakeRepo([make_row()])
    client = FakeClient(fail_paths={
        "/comments": FakeResp({"error": {"message": "no permission"}}, status=400),
    })
    result = social_worker.publish_due_social_posts(repo=repo, client=client,
                                                    sleep=NO_SLEEP)
    assert result["published"] == 1
    row = repo.rows[list(repo.rows)[0]]
    assert row["status"] == "published"
    assert "fb:" in row["comment_error"] and "ig:" in row["comment_error"]


def test_publish_no_image_not_due():
    repo = FakeRepo([make_row(image_data=None)])
    client = FakeClient()
    result = social_worker.publish_due_social_posts(repo=repo, client=client)
    assert result["published"] == 0
    assert client.calls == []


# ── Ingest tests ──────────────────────────────────────────────────────────────

def test_ingest_inserts(tmp_path):
    (tmp_path / "social-media-post-77-second-shift.md").write_text(POST77_FULL)
    old = tmp_path / "social-media-post-01-old.md"
    old.write_text(POST77_FULL)
    old_ts = (_now() - timedelta(days=30)).timestamp()
    os.utime(old, (old_ts, old_ts))

    repo = FakeRepo()
    r = social_worker.ingest_social_posts(repo=repo, posts_dir=str(tmp_path))
    assert r["inserted"] == 1
    row = repo.rows[list(repo.rows)[0]]
    assert row["source_file"] == "social-media-post-77-second-shift.md"
    assert row["status"] == "pending_review"
    assert row["photo_candidates"] == []

    # Second run: same file is a no-op (ON CONFLICT DO NOTHING)
    r2 = social_worker.ingest_social_posts(repo=repo, posts_dir=str(tmp_path))
    assert r2["inserted"] == 0
