"""Webhook routes — WhatsApp inbound + unsubscribe."""
from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query, Request

from app.utils.logger import get_logger

log = get_logger(__name__)
router = APIRouter(prefix="/webhook", tags=["webhooks"])


# ── WhatsApp Inbound Webhook ───────────────────────────────────────────
@router.post("/whatsapp")
async def whatsapp_webhook(request: Request, hub_verify_token: str = Query(...)) -> dict:
    """
    Meta Cloud API sends WhatsApp inbound messages here.
    Configured as: https://login.clientcompass.co.za/webhook/leadgen/whatsapp
    Nginx on hub proxies to this laptop:8000.
    """
    body = await request.json()
    log.info("whatsapp_webhook_received", body=body)

    # TODO (Phase 4): Process inbound message, detect replies
    return {"status": "ok"}


# ── Unsubscribe Endpoint ───────────────────────────────────────────────
@router.get("/unsubscribe")
async def unsubscribe(token: str = Query(...)) -> dict:
    """
    One-click unsubscribe — works without login.
    Token is a signed JWT: lead_id + expiry embedded.
    """
    import jwt

    from app.config import get_settings
    from app.db.session import session_scope
    from app.models import Lead, LeadEvent

    settings = get_settings()
    try:
        payload = jwt.decode(token, settings.unsubscribe_secret, algorithms=["HS256"])
    except jwt.ExpiredSignatureError:
        raise HTTPException(status_code=410, detail="Link expired")
    except jwt.InvalidTokenError:
        raise HTTPException(status_code=400, detail="Invalid token")

    lead_id = payload["lead_id"]
    async with session_scope() as session:
        await session.execute(
            __import__("sqlalchemy")
            .update(Lead)
            .where(Lead.id == lead_id)
            .values(status="opted_out")
        )
        session.add(LeadEvent(
            lead_id=lead_id,
            event_type="opted_out",
            payload={"source": "unsubscribe_link"},
        ))
        await session.commit()

    log.info("lead_unsubscribed", lead_id=lead_id)
    return {
        "status": "ok",
        "message": "You've been unsubscribed. You won't receive further messages from Client Compass.",
    }
