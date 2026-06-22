"""
AI Recommendation Agent.
Builds the booking recommendation from LIVE numbers only:
  - cross-vendor comparison results
  - live price timeline (future-date scan)
Uses OpenAI for the narrative when configured; otherwise a deterministic
analysis computed from the same live numbers. No static seasonal profiles.
"""
from __future__ import annotations

import datetime
import json

import structlog

from app.core.config.settings import get_settings
from app.schemas.models import (
    AIRecommendation,
    HotelComparison,
    HotelSearchRequest,
    PriceTimeline,
)

logger = structlog.get_logger(__name__)


def _build_context(
    req: HotelSearchRequest,
    comparisons: list[HotelComparison],
    timeline: PriceTimeline | None,
    market_avg: float,
    cheapest_price: float,
    cheapest_vendor: str,
    cheapest_hotel: str,
) -> str:
    days_until = (req.checkin - datetime.date.today()).days

    comp_lines = []
    for c in comparisons[:6]:
        vendor_list = ", ".join(
            f"{v.vendor} {v.price_per_night:.0f}"
            for v in sorted(c.vendor_prices, key=lambda x: x.price_per_night)
        )
        comp_lines.append(f"  - {c.canonical_name}: {vendor_list}")

    timeline_block = "  Not available (scan disabled or no data)"
    if timeline and timeline.points:
        rows = []
        for p in timeline.points:
            if p.sample_size:
                rows.append(
                    f"  - {p.label} (check-in {p.checkin}): avg {p.avg_price:.0f}, "
                    f"min {p.min_price:.0f}, median {p.median_price:.0f} "
                    f"({p.sample_size} live prices from {', '.join(p.vendors_used)})"
                )
            else:
                rows.append(f"  - {p.label}: no live data ({p.status})")
        timeline_block = "\n".join(rows) + (
            f"\n  Trend: {timeline.trend} ({timeline.trend_pct:+.1f}% future vs requested) | "
            f"Cheapest window: {timeline.cheapest_window} "
            f"(avg {timeline.cheapest_window_avg:.0f}) | confidence {timeline.confidence:.2f}"
        )

    target = f"Specific hotel requested: {req.hotel_name}" if req.hotel_name else "Whole-market search"

    return f"""
SEARCH REQUEST:
- Destination: {req.destination} | {target}
- Dates: {req.checkin} → {req.checkout} ({req.nights} nights), {req.adults} adults, {req.rooms} room(s)
- Budget cap: {f"{req.budget:.0f} {req.currency}/night" if req.budget > 0 else "none"}
- Days until check-in: {days_until}

LIVE MARKET PRICES (currency {req.currency}):
- Cheapest live offer: {cheapest_price:.0f}/night via {cheapest_vendor} ({cheapest_hotel})
- Market average: {market_avg:.0f}/night

CROSS-VENDOR COMPARISONS (same hotel, live prices per vendor):
{chr(10).join(comp_lines) or "  No multi-vendor matches found"}

LIVE FUTURE-DATE PRICE SCAN (real vendor quotes at shifted check-in dates):
{timeline_block}
""".strip()


async def get_ai_recommendation(
    req: HotelSearchRequest,
    comparisons: list[HotelComparison],
    timeline: PriceTimeline | None,
    market_avg: float,
    cheapest_price: float,
    cheapest_vendor: str,
    cheapest_hotel: str = "",
) -> AIRecommendation:
    settings = get_settings()
    context = _build_context(req, comparisons, timeline, market_avg,
                             cheapest_price, cheapest_vendor, cheapest_hotel)

    if settings.openai.api_key:
        try:
            return await _openai_recommendation(context, cheapest_price, cheapest_vendor, settings)
        except Exception as exc:
            logger.warning("openai_recommendation_failed", error=str(exc))

    return _deterministic_recommendation(req, timeline, market_avg, cheapest_price, cheapest_vendor)


async def _openai_recommendation(
    context: str,
    cheapest_price: float,
    cheapest_vendor: str,
    settings,
) -> AIRecommendation:
    from openai import AsyncOpenAI

    client = AsyncOpenAI(api_key=settings.openai.api_key, timeout=settings.openai.timeout)

    system_prompt = (
        "You are UbidStay's hotel pricing analyst. All numbers in the user message are "
        "LIVE vendor quotes pulled minutes ago. Analyze ONLY these numbers — never invent "
        "prices, seasons, or trends not present in the data. Be specific about money. "
        "The audience is a travel business deciding when and from which supplier to buy "
        "hotel inventory. Respond in JSON exactly as specified."
    )
    user_prompt = f"""{context}

Respond with a JSON object with these exact keys:
{{
  "action": "BOOK_NOW" | "WAIT" | "MONITOR",
  "urgency": "high" | "medium" | "low",
  "confidence": 0.0-1.0,
  "headline": "one sentence, max 15 words",
  "full_analysis": "3-4 paragraphs: current price assessment, future-scan insight (which scanned window is cheapest and by how much), vendor comparison insight (who is cheapest for the same property), and an exact buy/sell recommendation with margin guidance",
  "best_vendor": "vendor name",
  "best_price": float,
  "potential_saving": float,
  "best_time_to_book": "specific advice tied to the scanned windows",
  "avoid_periods": ["scanned windows that are expensive"],
  "tips": ["tip1", "tip2", "tip3"]
}}"""

    resp = await client.chat.completions.create(
        model=settings.openai.model,
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        max_tokens=settings.openai.max_tokens,
        temperature=settings.openai.temperature,
        response_format={"type": "json_object"},
    )
    data = json.loads(resp.choices[0].message.content)

    return AIRecommendation(
        action=data.get("action", "MONITOR"),
        urgency=data.get("urgency", "medium"),
        confidence=float(data.get("confidence", 0.75)),
        headline=data.get("headline", ""),
        full_analysis=data.get("full_analysis", ""),
        best_vendor=data.get("best_vendor", cheapest_vendor),
        best_price=float(data.get("best_price", cheapest_price)),
        potential_saving=float(data.get("potential_saving", 0)),
        best_time_to_book=data.get("best_time_to_book", ""),
        avoid_periods=[str(x) for x in data.get("avoid_periods", [])],
        tips=[str(t) for t in data.get("tips", [])],
        engine="openai",
    )


def _deterministic_recommendation(
    req: HotelSearchRequest,
    timeline: PriceTimeline | None,
    market_avg: float,
    cheapest_price: float,
    cheapest_vendor: str,
) -> AIRecommendation:
    """Rule-based decision computed from the same live numbers."""
    days_until = (req.checkin - datetime.date.today()).days
    saving_vs_avg = round(market_avg - cheapest_price, 2) if market_avg > 0 else 0.0
    saving_pct = round(saving_vs_avg / market_avg * 100, 1) if market_avg > 0 else 0.0

    trend = timeline.trend if timeline else "unknown"
    trend_pct = timeline.trend_pct if timeline else 0.0
    cheapest_window = timeline.cheapest_window if timeline else ""
    requested_is_cheapest = bool(timeline and cheapest_window == "Requested dates")
    potential_window_saving = timeline.potential_saving_pct if timeline else 0.0

    score = 0
    if saving_pct > 5:
        score += 2
    if requested_is_cheapest:
        score += 3
    if trend == "rising":
        score += 2
    elif trend == "falling":
        score -= 3
    if days_until <= 7:
        score += 2
    elif days_until <= 14:
        score += 1

    if score >= 4:
        action, urgency = "BOOK_NOW", "high"
    elif score <= 0:
        action, urgency = "WAIT", "low"
    else:
        action, urgency = "MONITOR", "medium"

    base_conf = timeline.confidence if timeline else 0.5
    confidence = round(min(0.95, base_conf + 0.05 * abs(score) / 2), 2)

    parts: list[str] = []
    parts.append(
        f"PRICE ASSESSMENT: The cheapest live offer is {cheapest_price:.0f} {req.currency}/night "
        f"via {cheapest_vendor}"
        + (f", {saving_pct:.1f}% below the live market average of {market_avg:.0f}."
           if saving_pct > 2 else f", roughly at the live market average ({market_avg:.0f}).")
    )
    if timeline and timeline.points:
        if requested_is_cheapest:
            parts.append(
                f"FUTURE SCAN: Live quotes at {len([p for p in timeline.points if p.sample_size])} "
                f"date windows show your requested dates are the cheapest — later windows average "
                f"{trend_pct:+.1f}% vs now ({trend} trend)."
            )
        else:
            parts.append(
                f"FUTURE SCAN: The cheapest scanned window is {cheapest_window} "
                f"(avg {timeline.cheapest_window_avg:.0f}/night, check-in "
                f"{timeline.cheapest_window_checkin}) — {potential_window_saving:.1f}% below your "
                f"requested dates. Overall forward trend is {trend} ({trend_pct:+.1f}%)."
            )
    else:
        parts.append("FUTURE SCAN: No live future-window data was available for this search.")
    parts.append(
        f"BOOKING TIMING: {days_until} days until check-in. "
        + ("Last-minute window — availability risk outweighs potential drops; act fast."
           if days_until <= 7 else
           "There is room to monitor, but the recommendation above already reflects the live forward curve.")
    )
    parts.append(
        f"FOR UBIDTOURS: Buy from {cheapest_vendor} at {cheapest_price:.0f}/night and list at "
        f"{cheapest_price * 1.12:.0f}–{cheapest_price * 1.18:.0f}/night for a 12–18% margin "
        f"against the {market_avg:.0f} live market average."
    )

    avoid = []
    if timeline:
        avoid = [p.label for p in timeline.points
                 if p.sample_size and p.avg_price > 0
                 and timeline.current_window_avg > 0
                 and p.avg_price > timeline.current_window_avg * 1.10]

    tips = [
        f"Cheapest vendor right now: {cheapest_vendor} at {cheapest_price:.0f} {req.currency}/night",
    ]
    if timeline and not requested_is_cheapest and cheapest_window:
        tips.append(
            f"Flexible dates? {cheapest_window} is {potential_window_saving:.1f}% cheaper "
            f"(check-in {timeline.cheapest_window_checkin})"
        )
    if trend == "rising":
        tips.append(f"Forward prices are rising ({trend_pct:+.1f}%) — locking in early protects margin")
    elif trend == "falling":
        tips.append(f"Forward prices are falling ({trend_pct:+.1f}%) — waiting may reduce cost")
    if saving_pct > 2:
        tips.append(f"Booking via {cheapest_vendor} saves {saving_vs_avg:.0f} {req.currency}/night vs market average")

    if action == "BOOK_NOW":
        headline = (f"Book now via {cheapest_vendor} at {cheapest_price:.0f} {req.currency}/night — "
                    f"forward prices are {trend}")
        timing = "Book within the next few days at the requested window"
    elif action == "WAIT":
        headline = (f"Wait — scanned windows show cheaper prices ahead "
                    f"({cheapest_window or 'later windows'})")
        timing = (f"Re-check around {timeline.cheapest_window_checkin}" if timeline and
                  timeline.cheapest_window_checkin else "Re-run this search in a few days")
    else:
        headline = (f"Competitive at {cheapest_price:.0f} {req.currency}/night via {cheapest_vendor} — "
                    f"monitor the forward curve")
        timing = "Monitor and re-run the scan every few days"

    return AIRecommendation(
        action=action,
        urgency=urgency,
        confidence=confidence,
        headline=headline,
        full_analysis="\n\n".join(parts),
        best_vendor=cheapest_vendor,
        best_price=cheapest_price,
        potential_saving=max(saving_vs_avg, 0.0),
        best_time_to_book=timing,
        avoid_periods=avoid,
        tips=tips,
        engine="deterministic",
    )
