"""
UbidStay Price Intelligence API
Run: uvicorn app.main:app --reload --port 8000
"""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from typing import AsyncGenerator

import structlog
from fastapi import FastAPI, Request, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import JSONResponse

from app.agents.orchestrator import get_orchestrator
from app.api.v1.endpoints.analytics import router as analytics_router
from app.api.v1.endpoints.health import router as health_router
from app.api.v1.endpoints.history import router as history_router
from app.api.v1.endpoints.search import router as search_router
from app.api.v1.endpoints.vendors import router as vendors_router
from app.api.v1.endpoints.watchlist import router as watchlist_router
from app.core.config.settings import get_settings

logger = structlog.get_logger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    settings = get_settings()
    orch = get_orchestrator()
    configured = [p.name for p in orch.providers if p.configured]
    missing = [p.name for p in orch.providers if not p.configured]
    logger.info(
        "ubidstay_starting",
        version=settings.app_version,
        env=settings.app_env,
        vendors_live=configured,
        vendors_not_configured=missing,
        history_enabled=settings.history.enabled,
        monitor_enabled=settings.monitor.enabled,
    )

    monitor_task: asyncio.Task | None = None
    if settings.monitor.enabled and settings.history.enabled:
        from app.services.monitor import monitor_loop
        monitor_task = asyncio.create_task(monitor_loop())

    yield

    if monitor_task:
        monitor_task.cancel()
        try:
            await monitor_task
        except (asyncio.CancelledError, Exception):
            pass
    await orch.close()
    logger.info("ubidstay_shutdown")


def create_app() -> FastAPI:
    settings = get_settings()

    app = FastAPI(
        title="UbidStay Price Intelligence API",
        version=settings.app_version,
        description="""
## UbidStay Hotel Price Intelligence System — LIVE DATA ONLY

Searches **SerpAPI (Google Hotels)**, **Booking.com**, **Expedia/Hotels.com**,
**HotelBeds**, **Amadeus**, and **Travelomatix** simultaneously and returns:

- **Per-vendor hotel lists** — every hotel each supplier is offering right now
- **Cross-vendor price comparison** — same hotel, different supplier prices
- **Live price timeline** — real forward price curve from future-date scans
- **AI recommendation** — when and from which supplier to buy

A vendor without API keys is reported as `not_configured` and excluded.
**There is no demo or mock data anywhere in this system.**
        """,
        docs_url="/docs",
        redoc_url="/redoc",
        openapi_url="/openapi.json",
        lifespan=lifespan,
    )

    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    app.add_middleware(GZipMiddleware, minimum_size=500)

    @app.exception_handler(Exception)
    async def global_error_handler(request: Request, exc: Exception) -> JSONResponse:
        logger.error("unhandled_error", error=str(exc), path=request.url.path)
        return JSONResponse(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            content={"error": "Internal server error", "detail": str(exc)},
        )

    app.include_router(health_router, tags=["Health"])
    app.include_router(search_router, prefix=settings.api_prefix, tags=["Price Intelligence"])
    app.include_router(vendors_router, prefix=settings.api_prefix, tags=["Vendors"])
    app.include_router(history_router, prefix=settings.api_prefix, tags=["Price History"])
    app.include_router(watchlist_router, prefix=settings.api_prefix, tags=["Watchlist"])
    app.include_router(analytics_router, prefix=settings.api_prefix, tags=["Analytics"])

    return app


app = create_app()
