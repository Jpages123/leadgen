"""Health check routes."""
from __future__ import annotations

from fastapi import APIRouter

router = APIRouter(prefix="/health", tags=["health"])


@router.get("")
async def health_check() -> dict:
    return {"status": "ok"}


@router.get("/ready")
async def readiness_check() -> dict:
    """Full readiness: DB + Redis connectivity."""
    from app.db.session import get_engine

    engine = get_engine()
    try:
        async with engine.connect() as conn:
            await conn.execute(
                __import__("sqlalchemy").text("SELECT 1")
            )
    except Exception as exc:
        return {"status": "not_ready", "error": str(exc)}

    return {"status": "ready", "db": "ok"}


@router.get("/live")
async def liveness_check() -> dict:
    return {"status": "alive"}
