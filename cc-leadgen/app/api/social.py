"""Social pipeline API — called by login-portal's social queue."""
from __future__ import annotations

from fastapi import APIRouter, Header, HTTPException
from pydantic import BaseModel

from app.config import get_settings
from app.social import pexels
from app.social.repo import SocialPostRepo
from app.utils.logger import get_logger

log = get_logger(__name__)
router = APIRouter(prefix="/api/social", tags=["social"])


class _SearchBody(BaseModel):
    post_id: str
    query: str


@router.post("/search")
async def social_search(
    body: _SearchBody,
    x_api_key: str = Header(..., description="API key for authentication"),
) -> dict:
    """Re-run a Pexels search for a post and replace its photo_candidates.

    Called by: login-portal admin route when the operator submits the
    "search" form on /admin/social-queue.

    Request body (JSON):  {"post_id": "uuid", "query": "2–5 word query"}

    Returns: {"ok": true, "count": N}
    """
    cfg = get_settings()
    if not cfg.api_key or x_api_key != cfg.api_key:
        raise HTTPException(status_code=401, detail="Invalid API key")

    query = (body.query or "").strip()
    if not body.post_id or not query:
        raise HTTPException(status_code=400, detail="post_id and query required")

    try:
        candidates = pexels.search_photos(query, orientation="portrait", per_page=9)
    except Exception as exc:
        log.warning("social_search_pexels_failed", post_id=body.post_id,
                    error=str(exc)[:200])
        raise HTTPException(status_code=502,
                            detail=f"Pexels search failed: {str(exc)[:200]}")

    try:
        updated = SocialPostRepo().replace_candidates(body.post_id, query, candidates)
    except Exception as exc:
        log.error("social_search_db_failed", post_id=body.post_id,
                  error=str(exc)[:200])
        raise HTTPException(status_code=502, detail="DB update failed")
    if not updated:
        raise HTTPException(status_code=404,
                            detail="post not found or not in an editable status")

    log.info("social_search_done", post_id=body.post_id, query=query,
             count=len(candidates))
    return {"ok": True, "count": len(candidates)}
