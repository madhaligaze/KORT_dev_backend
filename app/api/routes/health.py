from __future__ import annotations

from sqlalchemy import text

from fastapi import APIRouter

from app.core.config import settings
from app.core.database import get_engine

router = APIRouter()


@router.get("/health")
async def healthcheck() -> dict[str, str]:
    return {
        "status": "ok",
        "service": settings.app_name,
        "environment": settings.environment,
    }


@router.get("/health/live")
async def liveness() -> dict[str, str]:
    return {"status": "ok"}


@router.get("/health/ready")
async def readiness() -> dict:
    db_ok = _database_ready()
    return {
        "status": "ok" if db_ok else "degraded",
        "database": "ok" if db_ok else "error",
    }


def _database_ready() -> bool:
    try:
        with get_engine().connect() as connection:
            connection.execute(text("SELECT 1"))
        return True
    except Exception:
        return False
