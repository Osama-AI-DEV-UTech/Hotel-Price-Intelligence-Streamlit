"""Watchlist endpoints — create/list/delete watches, run live scans, alerts."""
from __future__ import annotations

import asyncio
from datetime import date
from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field, model_validator

from app.services.history import get_history_store
from app.services.monitor import run_all_watches, run_watch

router = APIRouter()


class WatchCreate(BaseModel):
    destination: str = Field(..., min_length=2)
    checkin: date
    checkout: date
    hotel_name: str | None = None
    adults: int = Field(default=2, ge=1, le=20)
    rooms: int = Field(default=1, ge=1, le=10)
    currency: str = Field(default="USD", min_length=3, max_length=3)
    target_price: float = Field(default=0.0, ge=0, description="Alert when best price ≤ this (0 = off)")

    @model_validator(mode="after")
    def _dates(self) -> "WatchCreate":
        if self.checkout <= self.checkin:
            raise ValueError("checkout must be after checkin")
        if self.checkin <= date.today():
            raise ValueError("checkin must be in the future for monitoring")
        return self


def _store():
    store = get_history_store()
    if store is None:
        raise HTTPException(status_code=503, detail="History store disabled (HISTORY_ENABLED=false)")
    return store


@router.post("/watchlist", summary="Add a destination/hotel to the watchlist")
async def create_watch(body: WatchCreate) -> dict[str, Any]:
    watch_id = await asyncio.to_thread(
        _store().watch_create,
        destination=body.destination,
        hotel_name=body.hotel_name,
        checkin=body.checkin.isoformat(),
        checkout=body.checkout.isoformat(),
        adults=body.adults,
        rooms=body.rooms,
        currency=body.currency.upper(),
        target_price=body.target_price,
    )
    return {"id": watch_id, "message": "Watch created — first scan runs on demand or next cycle"}


@router.get("/watchlist", summary="All watches with their latest live run")
async def list_watches() -> list[dict[str, Any]]:
    return await asyncio.to_thread(_store().watch_list)


@router.delete("/watchlist/{watch_id}", summary="Delete a watch and its run history")
async def delete_watch(watch_id: int) -> dict[str, Any]:
    store = _store()
    if not await asyncio.to_thread(store.watch_get, watch_id):
        raise HTTPException(status_code=404, detail=f"Watch {watch_id} not found")
    await asyncio.to_thread(store.watch_delete, watch_id)
    return {"deleted": watch_id}


@router.post("/watchlist/{watch_id}/run", summary="Run one watch live right now")
async def run_watch_now(watch_id: int) -> dict[str, Any]:
    watch = await asyncio.to_thread(_store().watch_get, watch_id)
    if not watch:
        raise HTTPException(status_code=404, detail=f"Watch {watch_id} not found")
    return await run_watch(watch)


@router.post("/watchlist/run-all", summary="Run every active watch live right now")
async def run_all_now() -> list[dict[str, Any]]:
    return await run_all_watches()


@router.get("/watchlist/{watch_id}/history", summary="Run history for one watch (price over time)")
async def watch_history(watch_id: int) -> list[dict[str, Any]]:
    store = _store()
    if not await asyncio.to_thread(store.watch_get, watch_id):
        raise HTTPException(status_code=404, detail=f"Watch {watch_id} not found")
    return await asyncio.to_thread(store.watch_runs, watch_id)


@router.get("/watchlist/alerts/recent", summary="Recent price-drop / target alerts")
async def recent_alerts() -> list[dict[str, Any]]:
    return await asyncio.to_thread(_store().recent_alerts)
