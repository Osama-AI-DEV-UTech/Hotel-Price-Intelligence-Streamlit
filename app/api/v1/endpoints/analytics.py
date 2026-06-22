"""Vendor performance analytics — computed from recorded live data."""
from __future__ import annotations

import asyncio
from typing import Any

from fastapi import APIRouter, HTTPException, Query

from app.services.history import get_history_store

router = APIRouter()


@router.get(
    "/analytics/vendors",
    summary="Vendor performance: win-rate, success-rate, response time, spreads",
    description="""
Computed entirely from YOUR recorded live searches:

- **win_rate_pct** — how often this vendor was the cheapest for a property
  matched across multiple vendors
- **success_rate_pct** — live calls that returned data (excludes not_configured)
- **avg_response_ms / avg_hotels_returned / avg_quote_price**
- **avg_saving_vs_worst** — average money saved by buying from this vendor
  instead of the most expensive one for the same hotel

The more searches you run, the sharper these numbers get.
    """,
)
async def vendor_analytics(days: int = Query(default=90, ge=1, le=730)) -> list[dict[str, Any]]:
    store = get_history_store()
    if store is None:
        raise HTTPException(status_code=503, detail="History store disabled (HISTORY_ENABLED=false)")
    return await asyncio.to_thread(store.vendor_analytics, days)
