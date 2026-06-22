"""
Watchlist Auto-Monitoring.
A background loop re-runs every active watch through the live orchestrator
(every MONITOR_INTERVAL_MINUTES) and records the result. Alerts fire when:
  - best price drops ≥ MONITOR_ALERT_DROP_PCT vs the previous run, or
  - best price reaches the watch's target_price.
All numbers come from live vendor calls — no synthetic data.
"""
from __future__ import annotations

import asyncio
from datetime import date
from typing import Any

import structlog

from app.core.config.settings import get_settings
from app.schemas.models import HotelSearchRequest
from app.services.history import get_history_store

logger = structlog.get_logger(__name__)


async def run_watch(watch: dict[str, Any]) -> dict[str, Any]:
    """Execute one watch live and record the run. Returns the run record."""
    from app.agents.orchestrator import get_orchestrator  # local import — avoid cycle

    store = get_history_store()
    if store is None:
        raise RuntimeError("History store disabled (HISTORY_ENABLED=false) — watchlist needs it")

    checkin = date.fromisoformat(watch["checkin"])
    if checkin <= date.today():
        await asyncio.to_thread(
            store.watch_deactivate, watch["id"],
            f"deactivated: check-in {watch['checkin']} has passed",
        )
        logger.info("watch_deactivated_past_date", watch_id=watch["id"])
        return {"watch_id": watch["id"], "note": "deactivated: check-in date has passed",
                "best_price": 0.0, "alert": False}

    req = HotelSearchRequest(
        destination=watch["destination"],
        checkin=checkin,
        checkout=date.fromisoformat(watch["checkout"]),
        adults=watch.get("adults") or 2,
        rooms=watch.get("rooms") or 1,
        currency=watch.get("currency") or "USD",
        hotel_name=watch.get("hotel_name") or None,
        include_timeline=False,            # monitoring runs stay cheap
    )
    resp = await get_orchestrator().search(req)

    best_price = 0.0
    best_vendor = ""
    best_hotel = ""
    if resp.summary.get("cheapest_option"):
        co = resp.summary["cheapest_option"]
        best_price = float(co.get("price_per_night") or 0)
        best_vendor = co.get("vendor", "")
        best_hotel = co.get("hotel_name", "")

    prev = await asyncio.to_thread(store.watch_last_run, watch["id"])
    change_pct = 0.0
    if prev and prev["best_price"] > 0 and best_price > 0:
        change_pct = round((best_price - prev["best_price"]) / prev["best_price"] * 100, 1)

    s = get_settings()
    target = float(watch.get("target_price") or 0)
    alert = False
    note = ""
    if best_price > 0:
        if change_pct <= -s.monitor.alert_drop_pct:
            alert = True
            note = f"Price dropped {abs(change_pct):.1f}% (now {best_price:.0f} via {best_vendor})"
        if target and best_price <= target:
            alert = True
            note = (note + " | " if note else "") + \
                   f"Target price {target:.0f} reached ({best_price:.0f} via {best_vendor})"
    elif resp.total_hotels_found == 0:
        note = "No live prices returned on this run"

    run = dict(best_price=best_price, best_vendor=best_vendor, best_hotel=best_hotel,
               market_avg=resp.market_average_price, hotels_found=resp.total_hotels_found,
               change_pct=change_pct, alert=alert, note=note)
    await asyncio.to_thread(store.watch_record_run, watch["id"], **run)
    logger.info("watch_run_recorded", watch_id=watch["id"], best=best_price,
                change_pct=change_pct, alert=alert)
    return {"watch_id": watch["id"], **run}


async def run_all_watches() -> list[dict[str, Any]]:
    store = get_history_store()
    if store is None:
        return []
    watches = await asyncio.to_thread(store.watch_list)
    results = []
    for w in watches:
        if not w.get("active"):
            continue
        try:
            results.append(await run_watch(w))
        except Exception as exc:  # one bad watch must not stop the loop
            logger.error("watch_run_failed", watch_id=w["id"], error=str(exc))
            results.append({"watch_id": w["id"], "error": str(exc)})
    return results


async def monitor_loop() -> None:
    """Background task started from app lifespan when MONITOR_ENABLED=true."""
    s = get_settings()
    interval = max(5, s.monitor.interval_minutes) * 60
    logger.info("monitor_loop_started", interval_minutes=s.monitor.interval_minutes)
    # small initial delay so startup isn't slowed by vendor calls
    await asyncio.sleep(30)
    while True:
        try:
            results = await run_all_watches()
            if results:
                logger.info("monitor_cycle_done", watches=len(results))
        except asyncio.CancelledError:
            logger.info("monitor_loop_stopped")
            raise
        except Exception as exc:
            logger.error("monitor_cycle_failed", error=str(exc))
        await asyncio.sleep(interval)
