"""Social posts pipeline — ingest vault posts + publish approved ones to FB/IG.

Two Celery tasks (beat-driven, see app.workers.celery_app):

  - ingest_social_posts (every 15 min)
      Scan settings.social_posts_dir (Obsidian vault Posts dir, mounted ro at
      /vault/posts) for social-media-post-*.md files modified within
      social_ingest_max_age_days, parse them, and INSERT new rows into
      admin_crm.social_posts (ON CONFLICT source_file DO NOTHING). The
      operator uploads the image manually in the portal Social Queue.

  - publish_due_social_posts (every 5 min)
      Guards first: rows stuck in 'publishing' >30min → failed; approved rows
      past scheduled_at by more than social_missed_window_hours → failed
      (never published). Then claims due rows atomically and publishes:
      FB Page photo → IG container → poll → media_publish → permalink →
      first comments. Partial progress (fb_post_id, ig_container_id, …) is
      persisted per step so a retried row only repeats the missing leg.
      Publish failures → status='failed' + ops alert email; never retried
      automatically.

Manual trigger:
    from app.workers.social import ingest_social_posts, publish_due_social_posts
    ingest_social_posts.delay() / publish_due_social_posts.delay()
"""
from __future__ import annotations

import smtplib
import time
from datetime import datetime, timedelta, timezone
from email.mime.text import MIMEText
from pathlib import Path

import httpx
from celery import shared_task

from app.config import get_settings
from app.social import meta, parser
from app.social.repo import SocialPostRepo
from app.utils.logger import get_logger

log = get_logger(__name__)

STUCK_PUBLISHING_MINUTES = 30
FAILED_QUEUE_URL = "https://login.clientcompass.co.za/admin/social-queue?status=failed"


# ── Ops alert email (same SMTP mechanics as email_draft.py) ──────────────────

def _send_alert_email(subject: str, body: str, *, settings=None) -> bool:
    """Send an ops alert email. Never raises — alerting must not crash tasks."""
    settings = settings or get_settings()
    if not (settings.smtp_host and settings.smtp_user and settings.smtp_pass):
        log.warning("social_alert_email_not_configured", subject=subject)
        return False
    try:
        from_addr = settings.smtp_from_email or settings.smtp_user
        msg = MIMEText(body, "plain", "utf-8")
        msg["From"] = f"{settings.smtp_from_name} <{from_addr}>"
        msg["To"] = settings.ops_alert_email
        msg["Subject"] = subject
        smtp = smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=20)
        smtp.starttls()
        smtp.login(settings.smtp_user, settings.smtp_pass)
        smtp.sendmail(from_addr, [settings.ops_alert_email], msg.as_string())
        smtp.quit()
        return True
    except Exception as exc:
        log.error("social_alert_email_failed", error=str(exc)[:300])
        return False


def _alert_publish_failure(post: dict, error: str, *, settings=None) -> None:
    angle = post.get("angle") or post.get("source_file") or post.get("id")
    _send_alert_email(
        f"Social post publish failed: {angle}",
        f"Post {post.get('id')} ({post.get('source_file')}) failed to publish.\n\n"
        f"Error: {error}\n\n"
        f"Review: {FAILED_QUEUE_URL}",
        settings=settings,
    )


# ── Ingest ───────────────────────────────────────────────────────────────────

@shared_task(bind=True, name="app.workers.social.tasks.ingest_social_posts")
def ingest_social_posts(self, repo=None, posts_dir: str | None = None) -> dict:
    """Scan the vault Posts dir → insert new rows + Pexels candidates."""
    settings = get_settings()
    root = Path(posts_dir or settings.social_posts_dir)
    if not root.is_dir():
        log.warning("social_ingest_dir_missing", dir=str(root))
        return {"status": "skipped", "reason": "posts_dir_missing", "dir": str(root)}

    cutoff = datetime.now(timezone.utc) - timedelta(days=settings.social_ingest_max_age_days)
    repo = repo or SocialPostRepo()

    scanned = 0
    inserted = 0
    for f in sorted(root.glob("social-media-post-*.md")):
        try:
            mtime = datetime.fromtimestamp(f.stat().st_mtime, timezone.utc)
        except OSError:
            continue
        if mtime < cutoff:
            continue
        scanned += 1

        parsed = parser.parse_post_file(f)
        if parsed is None:
            continue  # warning already logged by the parser

        try:
            new_id = repo.insert_post(parsed)
        except Exception as exc:
            log.error("social_insert_failed", file=f.name, error=str(exc)[:300])
            continue
        if not new_id:
            continue  # already ingested
        inserted += 1
        log.info("social_post_ingested", id=new_id, file=f.name,
                 post_number=parsed.post_number)

    log.info("social_ingest_done", scanned=scanned, inserted=inserted)
    return {"status": "ok", "scanned": scanned, "inserted": inserted}


# ── Publish ──────────────────────────────────────────────────────────────────

@shared_task(bind=True, name="app.workers.social.tasks.publish_due_social_posts")
def publish_due_social_posts(self, repo=None, client=None, sleep=None) -> dict:
    """Publish approved+due social posts to the FB Page + IG business account."""
    settings = get_settings()
    if not (settings.meta_page_id and settings.meta_ig_user_id
            and settings.meta_page_access_token):
        log.info("social_publish_not_configured")
        return {"status": "skipped", "reason": "not_configured"}

    repo = repo or SocialPostRepo()
    token = settings.meta_page_access_token

    def _clean(err) -> str:
        # Never persist the access token, even inside an exception message.
        return str(err).replace(token, "***")[:500]

    # Stuck guard — a worker that died mid-publish leaves 'publishing' rows.
    for row in repo.fail_stuck_publishing(timedelta(minutes=STUCK_PUBLISHING_MINUTES)):
        log.warning("social_post_stuck_failed", id=str(row.get("id")))
        _alert_publish_failure(
            row,
            row.get("last_error") or "stuck in publishing — verify on FB/IG before retrying",
            settings=settings,
        )

    # Missed window — never publish these; surface for the operator instead.
    for row in repo.fail_missed_window(timedelta(hours=settings.social_missed_window_hours)):
        log.warning("social_post_missed_window", id=str(row.get("id")))
        _alert_publish_failure(row, "missed publish window", settings=settings)

    published = failed = skipped = 0
    for row in repo.due_posts():
        post = repo.claim(row["id"])
        if not post:
            skipped += 1
            continue
        try:
            _publish_one(post, settings, repo, client=client, sleep=sleep)
            published += 1
            log.info("social_post_published", id=str(post["id"]))
        except Exception as exc:
            err = _clean(exc)
            log.error("social_post_publish_failed", id=str(post["id"]), error=err)
            try:
                repo.mark_failed(post["id"], err)
            except Exception as exc2:
                log.error("social_mark_failed_failed", id=str(post["id"]),
                          error=str(exc2)[:200])
            _alert_publish_failure(post, err, settings=settings)
            failed += 1

    log.info("social_publish_done", published=published, failed=failed, skipped=skipped)
    return {"status": "ok", "published": published, "failed": failed, "skipped": skipped}


def _publish_one(post: dict, settings, repo, *, client=None, sleep=None) -> None:
    """Publish one claimed row (status='publishing'). Raises on failure —
    the caller marks the row failed. IDs are persisted immediately after each
    step so a retried row only repeats the missing leg."""
    post_id = str(post["id"])
    image_url = f"{settings.social_image_base_url}/{post_id}.jpg"
    owns_client = client is None
    client = client or httpx.Client(timeout=meta.HTTP_TIMEOUT_S)
    try:
        # ── Facebook Page photo post ──
        if not post.get("fb_post_id"):
            fb_id = meta.fb_post_photo(client, settings, image_url, post["caption_fb"])
            repo.update_fields(post_id, fb_post_id=fb_id)
            post["fb_post_id"] = fb_id

        # ── Instagram container → poll → publish → permalink ──
        if not post.get("ig_media_id"):
            container_id = post.get("ig_container_id")
            if container_id:
                # Retry path: containers die with status ERROR and expire after
                # ~24h (lookup may itself fail). Any stale/broken container is
                # replaced with a fresh one rather than polled forever.
                try:
                    stale_status = meta.ig_container_status(client, settings,
                                                            container_id)
                except meta.MetaError:
                    stale_status = "LOOKUP_FAILED"
                if stale_status in ("ERROR", "EXPIRED", "LOOKUP_FAILED"):
                    log.warning("social_ig_container_stale", id=post_id,
                                container_id=container_id, status=stale_status)
                    container_id = None
            if not container_id:
                container_id = meta.ig_create_container(client, settings,
                                                        image_url,
                                                        post["caption_ig"])
                repo.update_fields(post_id, ig_container_id=container_id)
                post["ig_container_id"] = container_id
            meta.ig_wait_finished(client, settings, container_id,
                                  sleep=sleep or time.sleep)
            media_id = meta.ig_publish(client, settings, container_id)
            repo.update_fields(post_id, ig_media_id=media_id)
            post["ig_media_id"] = media_id
            try:
                permalink = meta.ig_permalink(client, settings, media_id)
                if permalink:
                    repo.update_fields(post_id, ig_permalink=permalink)
                    post["ig_permalink"] = permalink
            except Exception as exc:
                # Media is already live — a permalink lookup failure isn't
                # worth flipping the post to failed.
                log.warning("social_ig_permalink_failed", id=post_id,
                            error=str(exc)[:200])

        # ── First comments (failures are recorded, not fatal) ──
        comment_errors: list[str] = []
        first_comment = post.get("first_comment")
        if first_comment:
            if post.get("fb_post_id") and not post.get("fb_comment_id"):
                try:
                    cid = meta.fb_comment(client, settings, post["fb_post_id"],
                                          first_comment)
                    repo.update_fields(post_id, fb_comment_id=cid)
                    post["fb_comment_id"] = cid
                except Exception as exc:
                    log.warning("social_fb_comment_failed", id=post_id,
                                error=str(exc)[:200])
                    comment_errors.append(f"fb: {str(exc)[:200]}")
            if post.get("ig_media_id") and not post.get("ig_comment_id"):
                try:
                    cid = meta.ig_comment(client, settings, post["ig_media_id"],
                                          first_comment)
                    repo.update_fields(post_id, ig_comment_id=cid)
                    post["ig_comment_id"] = cid
                except Exception as exc:
                    log.warning("social_ig_comment_failed", id=post_id,
                                error=str(exc)[:200])
                    comment_errors.append(f"ig: {str(exc)[:200]}")
        if comment_errors:
            repo.update_fields(post_id, comment_error="; ".join(comment_errors)[:500])

        repo.mark_published(post_id)
    finally:
        if owns_client:
            client.close()
