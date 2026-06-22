"""
Price Timeline Agent — REAL forward price curve.

Instead of static seasonal profiles, this agent LIVE-queries the fastest
configured vendors at multiple future date windows (same stay length and
party size) and builds an actual market price curve from the returned
prices. Every number is a live vendor quote.
"""
from __future__ import annotations

import asyncio
import statistics
from datetime import timedelta

import structlog

from app.core.config.settings import get_settings
from app.providers.base.provider import BaseProvider
from app.schemas.models import (
    STATUS_API_ERROR,
    STATUS_NO_RESULTS,
    STATUS_SUCCESS,
    HotelSearchRequest,
    PriceTimeline,
    TimelinePoint,
)

logger = structlog.get_logger(__name__)


class PriceTimelineAgent:
    """Scans future date windows with live vendor calls and derives the trend."""

    def __init__(self, providers: list[BaseProvider]) -> None:
        self._providers = providers

    def _scan_providers(self) -> list[BaseProvider]:
        s = get_settings()
        usable = [p for p in self._providers if p.configured and p.supports_timeline]
        usable.sort(key=lambda p: p.priority)
        return usable[: max(1, s.timeline.max_providers)]

    async def run(self, req: HotelSearchRequest) -> PriceTimeline | None:
        s = get_settings()
        providers = self._scan_providers()
        if not providers:
            logger.warning("timeline_no_providers_available")
            return None

        offsets = s.timeline_offsets
        if 0 not in offsets:
            offsets = [0] + offsets

        sem = asyncio.Semaphore(max(1, s.timeline.concurrency))

        async def scan_point(offset: int) -> TimelinePoint:
            checkin = req.checkin + timedelta(days=offset)
            checkout = checkin + timedelta(days=req.nights)
            point_req = req.model_copy(update={
                "checkin": checkin,
                "checkout": checkout,
                "budget": 0.0,          # market view — no budget cap
                "rating": 0.0,
                "include_timeline": False,
            })
            label = "Requested dates" if offset == 0 else f"+{offset} days"
            prices: list[float] = []
            vendors_used: list[str] = []
            errors = 0

            async with sem:
                results = await asyncio.gather(
                    *(p.search(point_req) for p in providers),
                    return_exceptions=True,
                )
            for provider, res in zip(providers, results):
                if isinstance(res, Exception):
                    errors += 1
                    continue
                hotels, status, _err = res
                if status == STATUS_SUCCESS:
                    vendor_prices = [h.lowest_rate for h in hotels if h.lowest_rate]
                    if vendor_prices:
                        prices.extend(vendor_prices)
                        vendors_used.append(provider.name)
                elif status == STATUS_API_ERROR:
                    errors += 1

            if not prices:
                return TimelinePoint(
                    label=label, offset_days=offset,
                    checkin=checkin.isoformat(), checkout=checkout.isoformat(),
                    status=STATUS_API_ERROR if errors else STATUS_NO_RESULTS,
                )
            return TimelinePoint(
                label=label,
                offset_days=offset,
                checkin=checkin.isoformat(),
                checkout=checkout.isoformat(),
                min_price=round(min(prices), 2),
                avg_price=round(statistics.fmean(prices), 2),
                median_price=round(statistics.median(prices), 2),
                max_price=round(max(prices), 2),
                sample_size=len(prices),
                vendors_used=vendors_used,
                status=STATUS_SUCCESS,
            )

        points = list(await asyncio.gather(*(scan_point(o) for o in sorted(set(offsets)))))
        return self._analyze(req, points, [p.name for p in providers])

    # ── Trend analysis from live points ───────────────────────────────────────

    def _analyze(
        self,
        req: HotelSearchRequest,
        points: list[TimelinePoint],
        vendors: list[str],
    ) -> PriceTimeline:
        timeline = PriceTimeline(
            destination=req.destination,
            currency=req.currency,
            stay_nights=req.nights,
            points=points,
            vendors_used=vendors,
        )

        valid = [p for p in points if p.status == STATUS_SUCCESS and p.avg_price > 0]
        if not valid:
            timeline.best_booking_advice = (
                "Live future-price scan returned no data — rely on the current search results."
            )
            return timeline

        current = next((p for p in valid if p.offset_days == 0), valid[0])
        future = [p for p in valid if p.offset_days > 0]

        timeline.current_window_avg = current.avg_price

        cheapest = min(valid, key=lambda p: p.avg_price)
        expensive = max(valid, key=lambda p: p.avg_price)
        timeline.cheapest_window = cheapest.label
        timeline.cheapest_window_avg = cheapest.avg_price
        timeline.cheapest_window_checkin = cheapest.checkin
        timeline.most_expensive_window = expensive.label
        timeline.most_expensive_window_avg = expensive.avg_price

        if future and current.avg_price > 0:
            future_avg = statistics.fmean([p.avg_price for p in future])
            trend_pct = round((future_avg - current.avg_price) / current.avg_price * 100, 1)
            timeline.trend_pct = trend_pct
            timeline.trend = "rising" if trend_pct > 5 else "falling" if trend_pct < -5 else "stable"

        if current.avg_price > 0 and cheapest.avg_price > 0:
            timeline.potential_saving_pct = round(
                (current.avg_price - cheapest.avg_price) / current.avg_price * 100, 1
            )

        # Confidence from sample sizes + vendor coverage (live-data driven)
        total_samples = sum(p.sample_size for p in valid)
        timeline.confidence = round(min(
            0.95,
            0.30
            + 0.10 * len(vendors)
            + 0.05 * len(valid)
            + min(0.25, total_samples / 200),
        ), 2)

        # Advice derived purely from the live curve
        if cheapest.offset_days == 0:
            timeline.best_booking_advice = (
                f"Your requested dates are the cheapest scanned window "
                f"(avg {current.avg_price:.0f} {req.currency}/night). Book now — "
                f"prices average {timeline.trend_pct:+.1f}% across later windows."
            )
        else:
            timeline.best_booking_advice = (
                f"Cheapest scanned window is {cheapest.label} (check-in {cheapest.checkin}, "
                f"avg {cheapest.avg_price:.0f} {req.currency}/night) — "
                f"{timeline.potential_saving_pct:.1f}% below your requested dates. "
                f"If dates are flexible, shift to that window."
            )
        return timeline
