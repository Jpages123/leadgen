"""Response handler tasks — placeholder, filled in Phase 4."""
from __future__ import annotations

from celery import shared_task

from app.utils.logger import get_logger

log = get_logger(__name__)


@shared_task(bind=True, name="app.workers.response.tasks.poll_email_replies")
def poll_email_replies(self) -> dict:
    """Poll IMAP for email replies — implemented in Phase 4."""
    log.info("response_poll_started")
    return {"status": "ok", "replies_found": 0}
