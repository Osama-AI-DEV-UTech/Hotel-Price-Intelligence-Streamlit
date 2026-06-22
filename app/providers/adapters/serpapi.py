"""
SerpAPI Provider — Google Hotels engine.
Single fast GET request → ideal for the price timeline scan.
Docs: https://serpapi.com/google-hotels-api
"""
from __future__ import annotations

from typing import Any

import httpx
import structlog

from app.core.config.settings import get_settings
from app.providers.base.provider import BaseProvider, ProviderError
from app.schemas.models import HotelSearchRequest, RateDetail, VendorHotel

logger = structlog.get_logger(__name__)


class SerpApiProvider(BaseProvider):
    name = "serpapi"
    display_name = "SerpAPI (Google Hotels)"
    priority = 1
    supports_timeline = True

    def __init__(self) -> None:
        super().__init__()
        s = get_settings().serpapi
        self._api_key = s.api_key
        self._base_url = s.base_url

    def _create_client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            base_url=self._base_url,
            timeout=httpx.Timeout(connect=10.0, read=40.0, write=10.0, pool=5.0),
        )

    @property
    def configured(self) -> bool:
        return bool(self._api_key)

    async def fetch(self, req: HotelSearchRequest) -> list[VendorHotel]:
        query = (
            f"{req.hotel_name} {req.destination}"
            if req.hotel_name
            else f"hotels in {req.destination}"
        )
        params: dict[str, Any] = {
            "engine": "google_hotels",
            "q": query,
            "check_in_date": req.checkin.isoformat(),
            "check_out_date": req.checkout.isoformat(),
            "adults": req.adults,
            "children": req.children,
            "currency": req.currency,
            "gl": "us",
            "hl": "en",
            "api_key": self._api_key,
        }
        if req.children:
            params["children_ages"] = ",".join(str(a) for a in req.resolved_children_ages)
        if req.stars and 2 <= req.stars <= 5:
            params["hotel_class"] = ",".join(str(s) for s in range(req.stars, 6))
        if req.accommodation_type.lower() == "vacation_rental":
            params["vacation_rentals"] = "true"

        resp = await self._make_request("GET", "/search.json", params=params)

        if resp.get("error"):
            raise ProviderError(f"SerpAPI: {resp['error']}")

        return self._parse_response(resp, req)

    def _parse_response(self, resp: dict[str, Any], req: HotelSearchRequest) -> list[VendorHotel]:
        result: list[VendorHotel] = []
        for raw in resp.get("properties", []):
            rate = raw.get("rate_per_night") or {}
            total = raw.get("total_rate") or {}
            per_night = float(rate.get("extracted_lowest") or 0)
            total_price = float(total.get("extracted_lowest") or 0)
            if per_night <= 0 and total_price > 0:
                per_night = round(total_price / req.nights, 2)
            if per_night <= 0:
                continue
            if total_price <= 0:
                total_price = round(per_night * req.nights, 2)

            gps = raw.get("gps_coordinates") or {}
            overall = raw.get("overall_rating")
            guest_rating = round(float(overall) * 2, 1) if overall else None  # 5 → 10 scale

            stars = raw.get("extracted_hotel_class")
            if not stars:
                hc = str(raw.get("hotel_class", ""))
                stars = int(hc[0]) if hc[:1].isdigit() else None

            hotel = VendorHotel(
                vendor=self.name,
                vendor_hotel_id=str(raw.get("property_token", "") or raw.get("name", "")),
                name=raw.get("name", "Unknown Hotel"),
                address="",
                city=req.destination,
                country="",
                latitude=gps.get("latitude"),
                longitude=gps.get("longitude"),
                stars=stars,
                guest_rating=guest_rating,
                rating_count=raw.get("reviews"),
                amenities=[str(a) for a in (raw.get("amenities") or [])[:8]],
                images=[
                    img.get("thumbnail", "")
                    for img in (raw.get("images") or [])[:4]
                    if isinstance(img, dict)
                ],
                description=raw.get("description", ""),
                rates=[RateDetail(
                    rate_id=str(raw.get("property_token", "")),
                    room_type="Best Available",
                    meal_plan="Room Only",
                    price_per_night=per_night,
                    total_price=total_price,
                    currency=req.currency,
                    is_refundable=bool(raw.get("free_cancellation", False)),
                )],
            )

            # before-taxes rate as an additional real rate when present
            before_tax = float(rate.get("extracted_before_taxes_fees") or 0)
            if before_tax > 0 and abs(before_tax - per_night) > 0.01:
                hotel.rates.append(RateDetail(
                    rate_id=f"{raw.get('property_token', '')}-pretax",
                    room_type="Best Available (before taxes/fees)",
                    meal_plan="Room Only",
                    price_per_night=before_tax,
                    total_price=round(before_tax * req.nights, 2),
                    currency=req.currency,
                    is_refundable=bool(raw.get("free_cancellation", False)),
                ))

            hotel.compute_pricing()
            result.append(hotel)
        return result
