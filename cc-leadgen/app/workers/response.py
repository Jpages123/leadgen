"""Response handler tasks.

Superseded by ``app.workers.outreach.check_replies``, which is what the
Celery beat schedule actually calls (see ``response-poll`` in
``app.workers.celery_app``). This module is kept only so
``app.workers.response.tasks.poll_email_replies`` stays callable for any
external callers, but it just delegates.
"""
from __future__ import annotations

from celery import shared_task

from app.utils.logger import get_logger

log = get_logger(__name__)


@shared_task(bind=True, name="app.workers.response.tasks.poll_email_replies")
def poll_email_replies(self) -> dict:
    """Deprecated alias for ``app.workers.outreach.check_replies``."""
    from app.workers.outreach import check_replies
    return check_replies()
