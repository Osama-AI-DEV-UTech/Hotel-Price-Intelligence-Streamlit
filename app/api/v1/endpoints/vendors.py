"""Vendor status endpoint — which suppliers are configured for live data."""
from __future__ import annotations

from fastapi import APIRouter

from app.agents.orchestrator import get_orchestrator
from app.schemas.models import VendorStatus

router = APIRouter()


@router.get(
    "/vendors",
    response_model=list[VendorStatus],
    summary="List all vendors and their live-data configuration status",
)
async def list_vendors() -> list[VendorStatus]:
    orch = get_orchestrator()
    return [
        VendorStatus(
            name=p.name,
            display_name=p.display_name,
            configured=p.configured,
            priority=p.priority,
            supports_timeline=p.supports_timeline,
            note="" if p.configured else "Add API credentials in .env to enable live data",
        )
        for p in orch.providers
    ]
