"""Health check endpoints — vendor status is derived from the live registry."""
from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter
from pydantic import BaseModel

from app.agents.orchestrator import get_orchestrator
from app.core.config.settings import get_settings

router = APIRouter()


class HealthResponse(BaseModel):
    status: str
    version: str
    timestamp: datetime
    vendors_configured: dict[str, bool]
    openai_configured: bool
    timeline_enabled: bool


@router.get("/health", response_model=HealthResponse)
async def health() -> HealthResponse:
    s = get_settings()
    orch = get_orchestrator()
    return HealthResponse(
        status="healthy",
        version=s.app_version,
        timestamp=datetime.utcnow(),
        vendors_configured={p.name: p.configured for p in orch.providers},
        openai_configured=bool(s.openai.api_key),
        timeline_enabled=s.timeline.enabled,
    )


@router.get("/")
async def root() -> dict:
    return {
        "name": "UbidStay Price Intelligence API",
        "docs": "/docs",
        "health": "/health",
        "vendors": "GET /api/v1/vendors",
        "search": "POST /api/v1/hotels/search",
        "policy": "live vendor data only — no demo/mock data",
    }
