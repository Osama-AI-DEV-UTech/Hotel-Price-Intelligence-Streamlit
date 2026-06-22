"""
HotelBeds Provider — Booking API (availability).
Auth: X-Signature = SHA256(api_key + secret + unix_ts).
Destination is resolved via live geocoding (no hardcoded city codes) and
searched by geolocation + radius.
Docs: https://developer.hotelbeds.com/documentation/hotels/booking-api/
"""
from __future__ import annotations

import hashlib
import time
from typing import Any

import httpx
import structlog

from app.core.config.settings import get_settings
from app.providers.base.provider import BaseProvider
from app.schemas.models import HotelSearchRequest, RateDetail, VendorHotel
from app.services.geocoding import geocode

logger = structlog.get_logger(__name__)


class HotelBedsProvider(BaseProvider):
    name = "hotelbeds"
    display_name = "HotelBeds"
    priority = 2

    def __init__(self) -> None:
        super().__init__()
        s = get_settings().hotelbeds
        self._api_key = s.api_key
        self._api_secret = s.api_secret
        self._base_url = s.base_url

    def _create_client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            base_url=self._base_url,
            timeout=httpx.Timeout(connect=10.0, read=40.0, write=10.0, pool=5.0),
        )

    @property
    def configured(self) -> bool:
        return bool(self._api_key and self._api_secret)

    def _auth_headers(self) -> dict[str, str]:
        ts = str(int(time.time()))
        sig = hashlib.sha256(f"{self._api_key}{self._api_secret}{ts}".encode()).hexdigest()
        return {
            "Api-key": self._api_key,
            "X-Signature": sig,
            "Accept": "application/json",
            "Content-Type": "application/json",
        }

    async def fetch(self, req: HotelSearchRequest) -> list[VendorHotel]:
        loc = await geocode(req.destination)

        occupancy: dict[str, Any] = {
            "rooms": req.rooms,
            "adults": req.adults,
            "children": req.children,
        }
        if req.children:
            occupancy["paxes"] = [{"type": "CH", "age": age} for age in req.resolved_children_ages]

        payload: dict[str, Any] = {
            "stay": {
                "checkIn": req.checkin.isoformat(),
                "checkOut": req.checkout.isoformat(),
            },
            "occupancies": [occupancy],
            "geolocation": {
                "latitude": loc.latitude,
                "longitude": loc.longitude,
                "radius": req.radius or 10,
                "unit": "km",
            },
        }
        flt: dict[str, Any] = {}
        if req.stars:
            flt["minCategory"] = req.stars
        if req.budget:
            flt["maxRate"] = req.budget * req.nights
        if flt:
            payload["filter"] = flt

        resp = await self._make_request(
            "POST", "/hotel-api/1.0/hotels",
            json=payload,
            headers=self._auth_headers(),
        )
        return self._parse_response(resp, req)

    # ── Parsing ────────────────────────────────────────────────────────────────

    @staticmethod
    def _safe_str(val: Any, fallback: str = "") -> str:
        if val is None:
            return fallback
        if isinstance(val, str):
            return val
        if isinstance(val, dict):
            return str(val.get("content", fallback))
        return str(val)

    def _parse_response(self, resp: dict[str, Any], req: HotelSearchRequest) -> list[VendorHotel]:
        hotels: list[VendorHotel] = []
        for raw in resp.get("hotels", {}).get("hotels", []):
            coords = raw.get("coordinates") or {}
            vendor_hotel = VendorHotel(
                vendor=self.name,
                vendor_hotel_id=str(raw.get("code", "")),
                name=self._safe_str(raw.get("name"), "Unknown Hotel"),
                address=self._safe_str(raw.get("address"), ""),
                city=self._safe_str(raw.get("destinationName"), ""),
                country=self._safe_str(raw.get("countryCode"), ""),
                latitude=coords.get("latitude") if isinstance(coords, dict) else None,
                longitude=coords.get("longitude") if isinstance(coords, dict) else None,
                stars=self._parse_stars(self._safe_str(raw.get("categoryCode"), "")),
                guest_rating=self._parse_rating(raw),
                amenities=[
                    self._safe_str(f.get("description") if isinstance(f, dict) else f, "")
                    for f in raw.get("facilities", [])[:8]
                ],
                images=[
                    i.get("path", "") if isinstance(i, dict) else str(i)
                    for i in raw.get("images", [])[:4]
                ],
                description=self._safe_str(raw.get("description"), ""),
            )
            for room in raw.get("rooms", []):
                for rate in room.get("rates", []):
                    net = float(rate.get("net", 0) or 0)
                    if net <= 0:
                        continue
                    policies = rate.get("cancellationPolicies") or []
                    vendor_hotel.rates.append(RateDetail(
                        rate_id=rate.get("rateKey", ""),
                        room_type=self._safe_str(room.get("name"), "Standard Room"),
                        meal_plan=self._safe_str(rate.get("boardName"), "Room Only"),
                        price_per_night=round(net / req.nights, 2),
                        total_price=round(net, 2),
                        currency=resp.get("hotels", {}).get("currency")
                                 or rate.get("currency") or req.currency,
                        is_refundable=bool(policies),
                        cancellation_deadline=policies[0].get("from", "") if policies else "",
                    ))
            vendor_hotel.compute_pricing()
            if vendor_hotel.rates:
                hotels.append(vendor_hotel)
        return hotels

    def _parse_stars(self, code: str) -> int | None:
        for n in ("5", "4", "3", "2", "1"):
            if n in code:
                return int(n)
        return None

    def _parse_rating(self, raw: dict) -> float | None:
        reviews = raw.get("reviews", [])
        if reviews and isinstance(reviews[0], dict):
            v = reviews[0].get("rate", 0)
            try:
                return round(float(v) * 2, 1) if float(v) <= 5 else float(v)
            except (TypeError, ValueError):
                return None
        return None
