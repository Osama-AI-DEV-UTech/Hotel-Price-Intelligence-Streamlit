"""Price History endpoints — real recorded data from past live searches."""
from __future__ import annotations

import asyncio
from typing import Any

from fastapi import APIRouter, HTTPException, Query

from app.services.history import get_history_store

router = APIRouter()


def _store():
    store = get_history_store()
    if store is None:
        raise HTTPException(status_code=503, detail="History store disabled (HISTORY_ENABLED=false)")
    return store


@router.get("/history/destinations",
            summary="Destinations with recorded price history")
async def history_destinations() -> list[dict[str, Any]]:
    return await asyncio.to_thread(_store().destinations)


@router.get("/history/trend",
            summary="Market price trend over time for a destination (recorded live data)")
async def history_trend(
    destination: str = Query(..., min_length=2),
    days: int = Query(default=90, ge=1, le=730),
) -> dict[str, Any]:
    return await asyncio.to_thread(_store().trend, destination, days)


@router.get("/history/hotel",
            summary="Recorded price history for a specific hotel (per vendor)")
async def history_hotel(
    name: str = Query(..., min_length=2),
    destination: str | None = Query(default=None),
    days: int = Query(default=180, ge=1, le=730),
) -> list[dict[str, Any]]:
    return await asyncio.to_thread(_store().hotel_history, name, destination, days)
