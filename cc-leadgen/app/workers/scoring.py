"""Scoring worker — scores enriched leads, queues for outreach."""
from __future__ import annotations

from celery import shared_task

from app.config import get_settings
from app.db.sync_session import sync_session_scope
from app.models import Lead
from app.utils.logger import get_logger

log = get_logger(__name__)


def score_lead(lead: Lead) -> int:
    """Score a lead 0-100 based on reachability and business establishment.

    Optimised for the web-revamp ICP: established SA businesses with a bad
    website that we can actually contact for a pitch.

    Contact (reachability):
      email present             +25  primary outreach channel
      phone present             +20  cold call / follow-up
      whatsapp_number present   +10  secondary contact signal

    Website (pitch prerequisite):
      website present           +10  must have a site to revamp

    Business establishment (Google Places):
      google_review_count >= 20 +15  established SA tradesman sweet spot
      google_review_count >= 75 +10  bonus: very established (stacks with above)
      google_rating >= 4.0      +10  reputable, worth pitching

    Social:
      instagram_url present      +5  digitally aware business

    Penalties:
      review_count < 5 (known)  -25  fly-by-night, not worth pitching
      competitor_customer       -50  never contact
      no email AND no phone
        AND no whatsapp         hard discard (score = 0)

    Clamp result to 0-100.
    """
    # Hard discard -- completely unreachable
    if not lead.email and not lead.phone and not lead.whatsapp_number:
        return 0

    score = 0

    # -- Reachability ---------------------------------------------------------
    if lead.email:
        score += 25
    if lead.phone:
        score += 20
    if lead.whatsapp_number:
        score += 10

    # -- Pitch prerequisite ---------------------------------------------------
    if lead.website:
        score += 10

    # -- Business establishment -----------------------------------------------
    review_count = lead.google_review_count
    if review_count is not None:
        if review_count < 5:
            score -= 25  # fly-by-night penalty
        elif review_count >= 75:
            score += 25  # stacks: >= 20 tier (15) + >= 75 bonus (10)
        elif review_count >= 20:
            score += 15
    if lead.google_rating is not None and lead.google_rating >= 4.0:
        score += 10

    # -- Social ---------------------------------------------------------------
    if lead.instagram_url:
        score += 5

    # -- Hard penalty ---------------------------------------------------------
    if getattr(lead, "competitor_customer", False):
        score -= 50

    return max(0, min(score, 100))


def get_outreach_action(score: int, settings) -> str:
    """Map score to outreach action + status."""
    if score >= settings.score_outreach_high:
        return "high_priority"
    if score >= settings.score_outreach_medium:
        return "medium_priority"
    if score >= settings.score_outreach_low:
        return "low_priority"
    return "discard"


@shared_task(bind=True, name="app.workers.scoring.tasks.score_pending_leads")
def score_pending_leads(self, batch_size: int = 200) -> dict:
    """Score enriched leads and update their status + outreach queue.

    High priority (≥60):  status → outreach_queued immediately
    Medium (40–59):       status → enriched (second batch for later)
    Low (20–39):          status → enriched, hold 30 days
    <20:                  status → invalid, discard
    """
    settings = get_settings()

    with sync_session_scope() as session:
        leads = (
            session.query(Lead)
            .filter(Lead.status == "discovered")
            .filter(Lead.email.isnot(None) | Lead.phone.isnot(None) | Lead.whatsapp_number.isnot(None))
            .limit(batch_size)
            .all()
        )

    if not leads:
        log.info("scoring_queue_empty")
        return {"status": "ok", "leads_scored": 0}

    high = medium = low = discarded = 0
    scored_ids = []

    for lead in leads:
        score = score_lead(lead)
        lead.score = score
        action = get_outreach_action(score, settings)

        if action == "high_priority":
            lead.status = "outreach_queued"
            high += 1
        elif action == "medium_priority":
            lead.status = "enriched"
            medium += 1
        elif action == "low_priority":
            lead.status = "enriched"
            low += 1
        else:  # discard
            lead.status = "invalid"
            discarded += 1

        scored_ids.append(str(lead.id))

        with sync_session_scope() as session:
            db_lead = session.get(Lead, lead.id)
            if db_lead:
                db_lead.score = score
                db_lead.status = lead.status
                session.add(db_lead)

    log.info(
        "scoring_batch_done",
        total=len(leads),
        high=high,
        medium=medium,
        low=low,
        discarded=discarded,
    )

    return {
        "status": "ok",
        "leads_scored": len(leads),
        "high_priority": high,
        "medium_priority": medium,
        "low_priority": low,
        "discarded": discarded,
        "scored_ids": scored_ids,
    }