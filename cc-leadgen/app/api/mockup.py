"""Mockup dispatch API — called by login-portal to queue mockup generation."""
from __future__ import annotations

from fastapi import APIRouter, Header, HTTPException, BackgroundTasks
from pydantic import BaseModel

from app.config import get_settings
from app.db.sync_session import sync_session_scope
from app.models import Lead
from app.utils.logger import get_logger

log = get_logger(__name__)
router = APIRouter(prefix="/api/mockup", tags=["mockup"])

_settings = get_settings


def _dispatch_lead(lead_id: str) -> None:
    """Sync wrapper — runs in background task thread."""
    try:
        # Import lazily to avoid loading heavy deps on every request
        from app.workers.mockup_generator import generate_mockup
        generate_mockup.delay(lead_id)
    except Exception as exc:
        log.error("mockup_dispatch_background_error", lead_id=lead_id, error=str(exc))


@router.post("/dispatch")
async def dispatch_mockups(
    background_tasks: BackgroundTasks,
    lead_ids: list[str],
    x_api_key: str = Header(..., description="API key for authentication"),
) -> dict:
    """
    Queue mockup generation for a list of lead IDs.

    Called by: login-portal admin route when operator clicks "Generate Mockups"

    Flow:
      1. Validate API key
      2. Update each lead's mockup_status → 'queued'
      3. Dispatch generate_mockup.delay() in background
      4. Return count

    Request body (JSON):
      {"lead_ids": ["uuid1", "uuid2", ...]}

    Headers:
      X-Api-Key: <CC_LEADGEN_API_KEY>

    Returns:
      {"status": "ok", "dispatched": N, "lead_ids": [...]}
    """
    cfg = _settings()
    if not cfg.api_key or x_api_key != cfg.api_key:
        raise HTTPException(status_code=401, detail="Invalid API key")

    if not lead_ids:
        return {"status": "ok", "dispatched": 0, "lead_ids": []}

    dispatched = []
    errors = []

    with sync_session_scope() as session:
        for lead_id in lead_ids:
            lead = session.get(Lead, lead_id)
            if not lead:
                errors.append({"lead_id": lead_id, "error": "not_found"})
                continue
            if lead.mockup_status not in ("none", "failed"):
                errors.append({
                    "lead_id": lead_id,
                    "error": f"unexpected_status:{lead.mockup_status}"
                })
                continue
            lead.mockup_status = "queued"
            session.add(lead)
            dispatched.append(lead_id)

    # Dispatch Celery tasks in background (non-blocking)
    for lead_id in dispatched:
        background_tasks.add_task(_dispatch_lead, lead_id)

    log.info(
        "mockup_dispatch_request",
        dispatched=len(dispatched),
        errors=len(errors),
    )
    return {
        "status": "ok",
        "dispatched": len(dispatched),
        "lead_ids": dispatched,
        "errors": errors,
    }


# ── Generic task dispatch (used by email-draft "Send" button) ───────────────
def _dispatch_send_draft(draft_id: str) -> None:
    """Background task: dispatch send_email_draft Celery task."""
    try:
        from app.workers.email_draft import send_email_draft
        send_email_draft.delay(draft_id)
    except Exception as exc:
        log.error("send_draft_dispatch_background_error", draft_id=draft_id, error=str(exc))


class _SendDraftBody(BaseModel):
    draft_id: str


@router.post("/send-draft")
async def dispatch_send_draft(
    background_tasks: BackgroundTasks,
    body: _SendDraftBody,
    x_api_key: str = Header(..., description="API key for authentication"),
) -> dict:
    """
    Queue an email draft for sending via the email_draft worker.

    Called by: login-portal admin route when operator clicks "Send" on /admin/email-drafts/:id

    Request body (JSON):
      {"draft_id": "uuid"}

    Returns:
      {"status": "ok", "draft_id": "uuid"}
    """
    cfg = _settings()
    if not cfg.api_key or x_api_key != cfg.api_key:
        raise HTTPException(status_code=401, detail="Invalid API key")

    if not body.draft_id:
        raise HTTPException(status_code=400, detail="draft_id required")

    background_tasks.add_task(_dispatch_send_draft, body.draft_id)

    log.info("send_draft_dispatched", draft_id=body.draft_id)
    return {"status": "ok", "draft_id": body.draft_id}




# ── Audit PDF serve endpoint ─────────────────────────────────────────────────
from pathlib import Path as _Path
from fastapi.responses import FileResponse as _FileResponse


@router.get("/pdf/{filename}", summary="Serve audit PDF", tags=["mockup"])
async def serve_pdf(
    filename: str,
    x_api_key: str = Header(..., description="API key for authentication"),
) -> _FileResponse:
    """Stream a web-audit PDF to the caller (proxied from login-portal).

    Security:
      - Requires valid X-Api-Key header.
      - filename is sanitised: no path separators or traversal sequences.
      - Only .pdf files inside the durable reports directory are served.
    """
    cfg = _settings()
    if not cfg.api_key or x_api_key != cfg.api_key:
        raise HTTPException(status_code=401, detail="Invalid API key")

    # Sanitise — reject path traversal
    if "/" in filename or "\\" in filename or ".." in filename:
        raise HTTPException(status_code=400, detail="Invalid filename")
    if not filename.endswith(".pdf"):
        raise HTTPException(status_code=400, detail="Only PDF files are served here")

    from app.utils.report_assets import report_dir
    pdf_path = report_dir() / filename
    if not pdf_path.exists() or not pdf_path.is_file():
        log.warning("pdf_serve_not_found", filename=filename)
        raise HTTPException(status_code=404, detail=f"PDF not found: {filename}")

    log.info("pdf_served", filename=filename, size=pdf_path.stat().st_size)
    return _FileResponse(
        path=str(pdf_path),
        media_type="application/pdf",
        filename=filename,
    )
