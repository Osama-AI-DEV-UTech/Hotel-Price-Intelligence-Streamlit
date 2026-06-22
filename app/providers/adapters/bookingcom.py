"""
Booking.com Provider — via RapidAPI "booking-com15" (DataCrawler).
Flow: searchDestination (cached) → searchHotels.
Docs: https://rapidapi.com/DataCrawler/api/booking-com15
"""
from __future__ import annotations

from typing import Any

import httpx
import structlog

from app.core.config.settings import get_settings
from app.providers.base.provider import BaseProvider, ProviderError
from app.schemas.models import HotelSearchRequest, RateDetail, VendorHotel

logger = structlog.get_logger(__name__)


class BookingComProvider(BaseProvider):
    name = "bookingcom"
    display_name = "Booking.com (RapidAPI)"
    priority = 1
    supports_timeline = True

    def __init__(self) -> None:
        super().__init__()
        s = get_settings()
        self._api_key = s.bookingcom.rapidapi_key or s.rapidapi_key
        self._host = s.bookingcom.rapidapi_host
        self._base_url = s.bookingcom.base_url
        self._dest_cache: dict[str, tuple[str, str]] = {}

    def _create_client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            base_url=self._base_url,
            timeout=httpx.Timeout(connect=10.0, read=40.0, write=10.0, pool=5.0),
        )

    @property
    def configured(self) -> bool:
        return bool(self._api_key)

    def _headers(self) -> dict[str, str]:
        return {"X-RapidAPI-Key": self._api_key, "X-RapidAPI-Host": self._host}

    async def _resolve_destination(self, destination: str) -> tuple[str, str]:
        """Returns (dest_id, search_type) from the live destination lookup."""
        key = destination.strip().lower()
        if key in self._dest_cache:
            return self._dest_cache[key]
        resp = await self._make_request(
            "GET", "/api/v1/hotels/searchDestination",
            params={"query": destination},
            headers=self._headers(),
        )
        candidates = resp.get("data") or []
        if not candidates:
            raise ProviderError(f"Booking.com: no destination match for '{destination}'")
        # Prefer city-type results
        top = next(
            (c for c in candidates if str(c.get("search_type", "")).lower() == "city"),
            candidates[0],
        )
        dest_id = str(top.get("dest_id", ""))
        search_type = str(top.get("search_type", "CITY")).upper()
        if not dest_id:
            raise ProviderError(f"Booking.com: destination '{destination}' has no dest_id")
        self._dest_cache[key] = (dest_id, search_type)
        return dest_id, search_type

    async def fetch(self, req: HotelSearchRequest) -> list[VendorHotel]:
        dest_id, search_type = await self._resolve_destination(req.destination)

        params: dict[str, Any] = {
            "dest_id": dest_id,
            "search_type": search_type,
            "arrival_date": req.checkin.isoformat(),
            "departure_date": req.checkout.isoformat(),
            "adults": req.adults,
            "room_qty": req.rooms,
            "page_number": 1,
            "currency_code": req.currency,
            "units": "metric",
            "temperature_unit": "c",
            "languagecode": "en-us",
            "location": "US",
        }
        if req.children:
            params["children_age"] = ",".join(str(a) for a in req.resolved_children_ages)

        resp = await self._make_request(
            "GET", "/api/v1/hotels/searchHotels",
            params=params,
            headers=self._headers(),
        )

        if resp.get("status") is False:
            raise ProviderError(f"Booking.com: {resp.get('message', 'unknown API error')}")

        return self._parse_response(resp, req)

    def _parse_response(self, resp: dict[str, Any], req: HotelSearchRequest) -> list[VendorHotel]:
        result: list[VendorHotel] = []
        raw_hotels = (resp.get("data") or {}).get("hotels") or []

        for raw in raw_hotels:
            prop = raw.get("property") or {}
            price_block = prop.get("priceBreakdown") or {}
            gross = (price_block.get("grossPrice") or {})
            total = float(gross.get("value") or 0)
            if total <= 0:
                continue
            per_night = round(total / req.nights, 2)
            currency = gross.get("currency") or req.currency

            review_score = prop.get("reviewScore")          # already 0–10
            stars = prop.get("accuratePropertyClass") or prop.get("propertyClass")

            hotel = VendorHotel(
                vendor=self.name,
                vendor_hotel_id=str(raw.get("hotel_id", "") or prop.get("id", "")),
                name=prop.get("name", "Unknown Hotel"),
                address="",
                city=prop.get("wishlistName", req.destination),
                country=prop.get("countryCode", "").upper(),
                latitude=prop.get("latitude"),
                longitude=prop.get("longitude"),
                stars=int(stars) if stars else None,
                guest_rating=float(review_score) if review_score else None,
                rating_count=prop.get("reviewCount"),
                images=(prop.get("photoUrls") or [])[:4],
                rates=[RateDetail(
                    rate_id=str(raw.get("hotel_id", "")),
                    room_type="Best Available",
                    meal_plan="Room Only",
                    price_per_night=per_night,
                    total_price=round(total, 2),
                    currency=currency,
                    is_refundable=True,
                )],
            )

            # strikethrough price = real published higher rate
            strike = (price_block.get("strikethroughPrice") or {})
            strike_val = float(strike.get("value") or 0)
            if strike_val > total:
                hotel.rates.append(RateDetail(
                    rate_id=f"{raw.get('hotel_id', '')}-published",
                    room_type="Published Rate",
                    meal_plan="Room Only",
                    price_per_night=round(strike_val / req.nights, 2),
                    total_price=round(strike_val, 2),
                    currency=currency,
                    is_refundable=True,
                ))

            hotel.compute_pricing()
            result.append(hotel)
        return result
