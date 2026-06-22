"""
Agent Orchestration Engine.

Pipeline (every stage traced in the response's agent_trace):
  1. VendorSearchAgent   — all configured vendors queried live, in parallel
  2. HotelFilterAgent    — request filters + optional hotel_name targeting
  3. MatchingAgent       — same physical hotel matched across vendors
  4. PriceTimelineAgent  — live future-date scan (real price prediction)
  5. RecommendationAgent — OpenAI / deterministic analysis of live numbers

No stage ever produces synthetic data. A vendor without keys is reported
as "not_configured"; a failed call as "api_error" with the real message.
"""
from __future__ import annotations

import asyncio
import time
import uuid
from datetime import datetime

import structlog

from app.agents.matching import _name_similarity, match_hotels_across_vendors
from app.agents.recommendation import get_ai_recommendation
from app.agents.timeline import PriceTimelineAgent
from app.core.config.settings import get_settings
from app.providers.base.provider import BaseProvider
from app.providers.registry import build_providers
from app.schemas.models import (
    STATUS_NO_RESULTS,
    STATUS_SUCCESS,
    AgentStep,
    HotelSearchRequest,
    PriceComparisonResponse,
    VendorHotel,
    VendorResultSet,
)

logger = structlog.get_logger(__name__)

HOTEL_NAME_MATCH_THRESHOLD = 0.45


class AgentOrchestrator:
    def __init__(self) -> None:
        self.providers: list[BaseProvider] = build_providers()
        self._timeline_agent = PriceTimelineAgent(self.providers)

    # ── Public API ─────────────────────────────────────────────────────────────

    async def search(self, req: HotelSearchRequest) -> PriceComparisonResponse:
        start = time.monotonic()
        search_id = str(uuid.uuid4())[:8].upper()
        trace: list[AgentStep] = []

        logger.info("search_started", search_id=search_id, destination=req.destination,
                    hotel_name=req.hotel_name, checkin=str(req.checkin))

        # 1 — parallel live vendor search
        t0 = time.monotonic()
        vendor_results = await self._search_all_vendors(req)
        live_vendors = [v.vendor for v in vendor_results if v.search_status == STATUS_SUCCESS]
        trace.append(AgentStep(
            agent="VendorSearchAgent",
            duration_ms=int((time.monotonic() - t0) * 1000),
            detail=f"{len(live_vendors)}/{len(vendor_results)} vendors returned live data "
                   f"({', '.join(live_vendors) or 'none'})",
        ))

        # 2 — filters + optional specific-hotel targeting
        t0 = time.monotonic()
        vendor_results = self._apply_filters(req, vendor_results)
        all_hotels: list[VendorHotel] = [h for vr in vendor_results for h in vr.hotels]
        trace.append(AgentStep(
            agent="HotelFilterAgent",
            duration_ms=int((time.monotonic() - t0) * 1000),
            detail=(f"hotel_name='{req.hotel_name}' matched {len(all_hotels)} vendor entries"
                    if req.hotel_name else f"{len(all_hotels)} hotels after filters"),
        ))

        # 3 — market stats + cross-vendor matching
        t0 = time.monotonic()
        priced = [h for h in all_hotels if h.lowest_rate]
        prices = [h.lowest_rate for h in priced]
        market_avg = round(sum(prices) / len(prices), 2) if prices else 0.0
        market_min = round(min(prices), 2) if prices else 0.0
        market_max = round(max(prices), 2) if prices else 0.0
        cheapest_hotel = min(priced, key=lambda h: h.lowest_rate, default=None)
        most_expensive_hotel = max(priced, key=lambda h: h.lowest_rate, default=None)
        comparisons = match_hotels_across_vendors(all_hotels)
        multi = len([c for c in comparisons if len(c.vendor_prices) > 1])
        trace.append(AgentStep(
            agent="MatchingAgent",
            duration_ms=int((time.monotonic() - t0) * 1000),
            detail=f"{len(comparisons)} properties, {multi} matched across multiple vendors",
        ))

        # 4 — live future-date price scan
        timeline = None
        if req.include_timeline and get_settings().timeline.enabled:
            t0 = time.monotonic()
            try:
                timeline = await self._timeline_agent.run(req)
                detail = (
                    f"{len([p for p in timeline.points if p.sample_size])} live windows, "
                    f"trend={timeline.trend} ({timeline.trend_pct:+.1f}%)"
                    if timeline else "no timeline-capable vendor configured"
                )
                trace.append(AgentStep(
                    agent="PriceTimelineAgent",
                    duration_ms=int((time.monotonic() - t0) * 1000),
                    status="completed" if timeline else "skipped",
                    detail=detail,
                ))
            except Exception as exc:  # timeline failure must not kill the search
                logger.error("timeline_failed", error=str(exc))
                trace.append(AgentStep(
                    agent="PriceTimelineAgent", status="failed",
                    duration_ms=int((time.monotonic() - t0) * 1000), detail=str(exc),
                ))
        else:
            trace.append(AgentStep(agent="PriceTimelineAgent", status="skipped",
                                   detail="disabled by request/config"))

        # 5 — AI recommendation (only if we have live prices to analyse)
        ai_reco = None
        if cheapest_hotel:
            t0 = time.monotonic()
            try:
                ai_reco = await get_ai_recommendation(
                    req=req,
                    comparisons=comparisons,
                    timeline=timeline,
                    market_avg=market_avg,
                    cheapest_price=cheapest_hotel.lowest_rate or 0.0,
                    cheapest_vendor=cheapest_hotel.vendor,
                    cheapest_hotel=cheapest_hotel.name,
                )
                trace.append(AgentStep(
                    agent="RecommendationAgent",
                    duration_ms=int((time.monotonic() - t0) * 1000),
                    detail=f"engine={ai_reco.engine}, action={ai_reco.action}",
                ))
            except Exception as exc:
                logger.error("recommendation_failed", error=str(exc))
                trace.append(AgentStep(
                    agent="RecommendationAgent", status="failed",
                    duration_ms=int((time.monotonic() - t0) * 1000), detail=str(exc),
                ))
        else:
            trace.append(AgentStep(agent="RecommendationAgent", status="skipped",
                                   detail="no live prices returned by any vendor"))

        total_ms = int((time.monotonic() - start) * 1000)

        summary = {
            "cheapest_option": {
                "vendor": cheapest_hotel.vendor if cheapest_hotel else "",
                "price_per_night": cheapest_hotel.lowest_rate if cheapest_hotel else 0.0,
                "hotel_name": cheapest_hotel.name if cheapest_hotel else "",
            },
            "market_average": market_avg,
            "vendors_live": len(live_vendors),
            "vendors_total": len(vendor_results),
            "total_hotels_found": len(all_hotels),
            "hotels_across_multiple_vendors": multi,
            "forward_trend": timeline.trend if timeline else "unknown",
            "recommendation": ai_reco.action if ai_reco else "NO_DATA",
            "potential_saving_vs_avg": round(market_avg - (cheapest_hotel.lowest_rate or 0), 2)
                                       if cheapest_hotel and market_avg > 0 else 0.0,
        }

        response = PriceComparisonResponse(
            search_id=search_id,
            destination=req.destination,
            hotel_name=req.hotel_name,
            checkin=str(req.checkin),
            checkout=str(req.checkout),
            nights=req.nights,
            adults=req.adults,
            rooms=req.rooms,
            currency=req.currency,
            searched_at=datetime.utcnow(),
            total_search_time_ms=total_ms,
            vendors=vendor_results,
            total_hotels_found=len(all_hotels),
            comparisons=comparisons,
            market_lowest_price=market_min,
            market_highest_price=market_max,
            market_average_price=market_avg,
            cheapest_vendor_overall=cheapest_hotel.vendor if cheapest_hotel else "",
            cheapest_hotel_overall=cheapest_hotel.name if cheapest_hotel else "",
            most_expensive_vendor_overall=most_expensive_hotel.vendor if most_expensive_hotel else "",
            price_timeline=timeline,
            ai_recommendation=ai_reco,
            agent_trace=trace,
            summary=summary,
        )
        logger.info("search_completed", search_id=search_id,
                    hotels=len(all_hotels), ms=total_ms)

        # Record to the price-history store (fire-and-forget — a history
        # failure must never break a live search)
        try:
            from app.services.history import get_history_store
            store = get_history_store()
            if store is not None:
                asyncio.create_task(store.record_search_async(req, response))
        except Exception as exc:
            logger.error("history_dispatch_failed", error=str(exc))

        return response

    # ── Stage 1: parallel vendor fan-out ──────────────────────────────────────

    async def _search_all_vendors(self, req: HotelSearchRequest) -> list[VendorResultSet]:
        async def search_one(provider: BaseProvider) -> VendorResultSet:
            start = time.monotonic()
            hotels, status, error = await provider.search(req)
            return VendorResultSet(
                vendor=provider.name,
                vendor_display_name=provider.display_name,
                hotels_found=len(hotels),
                hotels=hotels,
                search_status=status,
                response_time_ms=int((time.monotonic() - start) * 1000),
                error=error,
            )

        results = await asyncio.gather(*(search_one(p) for p in self.providers))
        return list(results)

    # ── Stage 2: filters + hotel_name targeting ───────────────────────────────

    def _apply_filters(
        self, req: HotelSearchRequest, vendor_results: list[VendorResultSet],
    ) -> list[VendorResultSet]:
        for vr in vendor_results:
            if vr.search_status != STATUS_SUCCESS:
                continue
            hotels = vr.hotels
            if req.hotel_name:
                hotels = [
                    h for h in hotels
                    if _name_similarity(h.name, req.hotel_name) >= HOTEL_NAME_MATCH_THRESHOLD
                    or req.hotel_name.lower() in h.name.lower()
                ]
            if req.budget > 0:
                hotels = [h for h in hotels if h.lowest_rate is None or h.lowest_rate <= req.budget]
            if req.stars > 0:
                hotels = [h for h in hotels if h.stars is None or h.stars >= req.stars]
            if req.rating > 0:
                hotels = [h for h in hotels if h.guest_rating is None or h.guest_rating >= req.rating]
            vr.hotels = hotels
            vr.hotels_found = len(hotels)
            if not hotels:
                vr.search_status = STATUS_NO_RESULTS
                if req.hotel_name and not vr.error:
                    vr.error = f"No property matching '{req.hotel_name}' in live results"
        return vendor_results

    async def close(self) -> None:
        await asyncio.gather(*(p.close() for p in self.providers), return_exceptions=True)


# ── Singleton ──────────────────────────────────────────────────────────────────
_orchestrator: AgentOrchestrator | None = None


def get_orchestrator() -> AgentOrchestrator:
    global _orchestrator
    if _orchestrator is None:
        _orchestrator = AgentOrchestrator()
    return _orchestrator
