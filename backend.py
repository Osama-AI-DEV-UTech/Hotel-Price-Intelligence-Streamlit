"""
UbidStay — In-Process Backend Bridge.

This module replaces the old "requests.get/post() -> FastAPI over HTTP"
client. Instead, it imports the SAME endpoint functions that used to run
behind `uvicorn app.main:app` and calls them directly as plain Python
coroutines, in the same process as Streamlit. There is no localhost HTTP
hop anywhere — this file is the UI's only data source.

Why this is safe to reuse 1:1:
  - Every FastAPI endpoint below is a plain `async def` with normal
    parameters (no Request/Depends/BackgroundTasks). Calling them directly
    runs the exact same business logic, the exact same validation, and
    raises the exact same fastapi.HTTPException on bad input — we just
    translate that into a plain Python exception for the UI's try/except.
  - Pydantic response models are converted to plain dict/list with
    `.model_dump(mode="json")`, which is byte-for-byte the same shape
    FastAPI used to serialize over the wire — so nothing in ui.py's
    rendering logic needs to change.

Event-loop handling:
  Streamlit reruns this script top-to-bottom on every interaction, but the
  Python process (and therefore the `app.*` singletons: the orchestrator,
  its httpx clients, the SQLite history store) stays alive across reruns.
  Each call below gets its own fresh `asyncio` event loop. Before that loop
  closes we (a) await any fire-and-forget tasks the orchestrator spawned
  (e.g. price-history recording) so they aren't dropped mid-flight, and
  (b) close the orchestrator's vendor HTTP clients so the next call (which
  will run on a brand-new loop) is forced to lazily recreate them instead
  of reusing a client object that belongs to a now-dead loop.
"""
from __future__ import annotations

import asyncio
from typing import Any

from fastapi import HTTPException
from pydantic import ValidationError

# ── Endpoint functions — identical logic to the FastAPI service ───────────────
from app.api.v1.endpoints.analytics import vendor_analytics as _vendor_analytics
from app.api.v1.endpoints.history import (
    history_destinations as _history_destinations,
    history_hotel as _history_hotel,
    history_trend as _history_trend,
)
from app.api.v1.endpoints.search import search_hotels as _search_hotels
from app.api.v1.endpoints.vendors import list_vendors as _list_vendors
from app.api.v1.endpoints.watchlist import (
    WatchCreate,
    create_watch as _create_watch,
    delete_watch as _delete_watch,
    list_watches as _list_watches,
    recent_alerts as _recent_alerts,
    run_all_now as _run_all_now,
    run_watch_now as _run_watch_now,
    watch_history as _watch_history,
)
from app.agents.orchestrator import get_orchestrator
from app.schemas.models import HotelSearchRequest


class BackendError(RuntimeError):
    """Raised for any failure — carries the same message the API used to return."""


# ── Event loop plumbing ─────────────────────────────────────────────────────────

def _run(coro: Any) -> Any:
    """Run one coroutine to completion on a brand-new, throwaway event loop."""
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


async def _settle_background_work() -> None:
    """
    Let any fire-and-forget tasks (history recording) finish, then close the
    orchestrator's vendor HTTP clients so they get rebuilt fresh on the next
    (new) event loop instead of being reused across loops.
    """
    current = asyncio.current_task()
    pending = [t for t in asyncio.all_tasks() if t is not current]
    if pending:
        await asyncio.gather(*pending, return_exceptions=True)
    await get_orchestrator().close()


def _unwrap(exc: Exception) -> BackendError:
    if isinstance(exc, HTTPException):
        return BackendError(str(exc.detail))
    if isinstance(exc, ValidationError):
        return BackendError("; ".join(e["msg"] for e in exc.errors()) or str(exc))
    return BackendError(str(exc))


# ── Vendors ──────────────────────────────────────────────────────────────────

def vendors() -> list[dict[str, Any]]:
    async def _go():
        result = await _list_vendors()
        return [v.model_dump(mode="json") for v in result]
    try:
        return _run(_go())
    except Exception as exc:
        raise _unwrap(exc) from exc


# ── Hotel search (the main engine) ──────────────────────────────────────────

def search(payload: dict[str, Any]) -> dict[str, Any]:
    async def _go():
        try:
            req = HotelSearchRequest(**payload)
        except ValidationError as exc:
            raise _unwrap(exc) from exc
        resp = await _search_hotels(req)
        await _settle_background_work()
        return resp.model_dump(mode="json")
    try:
        return _run(_go())
    except BackendError:
        raise
    except Exception as exc:
        raise _unwrap(exc) from exc


# ── Price history ────────────────────────────────────────────────────────────

def history_destinations() -> list[dict[str, Any]]:
    try:
        return _run(_history_destinations())
    except Exception as exc:
        raise _unwrap(exc) from exc


def history_trend(destination: str, days: int = 90) -> dict[str, Any]:
    try:
        return _run(_history_trend(destination=destination, days=days))
    except Exception as exc:
        raise _unwrap(exc) from exc


def history_hotel(name: str, destination: str | None = None, days: int = 180) -> list[dict[str, Any]]:
    try:
        return _run(_history_hotel(name=name, destination=destination, days=days))
    except Exception as exc:
        raise _unwrap(exc) from exc


# ── Watchlist ────────────────────────────────────────────────────────────────

def watchlist_create(payload: dict[str, Any]) -> dict[str, Any]:
    async def _go():
        try:
            body = WatchCreate(**payload)
        except ValidationError as exc:
            raise _unwrap(exc) from exc
        return await _create_watch(body)
    try:
        return _run(_go())
    except BackendError:
        raise
    except Exception as exc:
        raise _unwrap(exc) from exc


def watchlist_list() -> list[dict[str, Any]]:
    try:
        return _run(_list_watches())
    except Exception as exc:
        raise _unwrap(exc) from exc


def watchlist_alerts_recent() -> list[dict[str, Any]]:
    try:
        return _run(_recent_alerts())
    except Exception as exc:
        raise _unwrap(exc) from exc


def watchlist_run_all() -> list[dict[str, Any]]:
    async def _go():
        result = await _run_all_now()
        await _settle_background_work()
        return result
    try:
        return _run(_go())
    except Exception as exc:
        raise _unwrap(exc) from exc


def watchlist_run_one(watch_id: int) -> dict[str, Any]:
    async def _go():
        result = await _run_watch_now(watch_id)
        await _settle_background_work()
        return result
    try:
        return _run(_go())
    except Exception as exc:
        raise _unwrap(exc) from exc


def watchlist_delete(watch_id: int) -> dict[str, Any]:
    try:
        return _run(_delete_watch(watch_id))
    except Exception as exc:
        raise _unwrap(exc) from exc


def watchlist_history(watch_id: int) -> list[dict[str, Any]]:
    try:
        return _run(_watch_history(watch_id))
    except Exception as exc:
        raise _unwrap(exc) from exc


# ── Vendor analytics ─────────────────────────────────────────────────────────

def analytics_vendors(days: int = 90) -> list[dict[str, Any]]:
    try:
        return _run(_vendor_analytics(days))
    except Exception as exc:
        raise _unwrap(exc) from exc
