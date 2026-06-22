"""
Travelomatix Provider — B2B Hotel API (TravelNext v6 style).
Travelomatix is a partner platform; the exact host/path comes from your
account manager, so base_url + search_path are fully configurable via .env.
Parsing is defensive across the known response shapes (itineraries /
HotelSearchResult variants).
"""
from __future__ import annotations

from typing import Any

import httpx
import structlog

from app.core.config.settings import get_settings
from app.providers.base.provider import BaseProvider, ProviderError
from app.schemas.models import HotelSearchRequest, RateDetail, VendorHotel
from app.services.geocoding import geocode

logger = structlog.get_logger(__name__)


class TravelomatixProvider(BaseProvider):
    name = "travelomatix"
    display_name = "Travelomatix"
    priority = 4
    supports_timeline = True

    def __init__(self) -> None:
        super().__init__()
        s = get_settings().travelomatix
        self._user_id = s.user_id
        self._user_password = s.user_password
        self._access = s.access
        self._base_url = s.base_url.rstrip("/")
        self._search_path = s.search_path
        self._ip = s.ip_address

    def _create_client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            base_url=self._base_url,
            timeout=httpx.Timeout(connect=10.0, read=45.0, write=10.0, pool=5.0),
        )

    @property
    def configured(self) -> bool:
        return bool(self._user_id and self._user_password)

    async def fetch(self, req: HotelSearchRequest) -> list[VendorHotel]:
        loc = await geocode(req.destination)

        occupancy: list[dict[str, Any]] = []
        for room_no in range(1, req.rooms + 1):
            room: dict[str, Any] = {"room_no": room_no, "adult": req.adults, "child": 0}
            if room_no == 1 and req.children:
                room["child"] = req.children
                room["child_age"] = req.resolved_children_ages
            occupancy.append(room)

        payload: dict[str, Any] = {
            "user_id": self._user_id,
            "user_password": self._user_password,
            "access": self._access,
            "ip_address": self._ip,
            "requiredCurrency": req.currency,
            "nationality": loc.country_code or "US",
            "checkin": req.checkin.isoformat(),
            "checkout": req.checkout.isoformat(),
            "city_name": loc.city,
            "country_name": loc.country,
            "occupancy": occupancy,
        }

        resp = await self._make_request(
            "POST", self._search_path,
            json=payload,
            headers={"Content-Type": "application/json", "Accept": "application/json"},
        )
        return self._parse_response(resp, req)

    # ── Defensive parsing across known partner response shapes ────────────────

    @staticmethod
    def _first(d: dict[str, Any], *keys: str, default: Any = None) -> Any:
        for k in keys:
            if k in d and d[k] not in (None, ""):
                return d[k]
        return default

    def _extract_hotel_list(self, resp: dict[str, Any]) -> list[dict[str, Any]]:
        status = resp.get("status")
        if isinstance(status, dict):
            code = str(self._first(status, "Code", "code", "status_code", default=""))
            if code and code not in ("200", "1", "success", "Success"):
                desc = self._first(status, "Description", "description", "message", default="")
                raise ProviderError(f"Travelomatix: status {code} — {desc}")
        if resp.get("Error") or resp.get("error"):
            err = resp.get("Error") or resp.get("error")
            msg = err.get("ErrorMessage", str(err)) if isinstance(err, dict) else str(err)
            raise ProviderError(f"Travelomatix: {msg}")

        for path in (
            ("itineraries",),
            ("Itineraries",),
            ("hotels",),
            ("HotelSearchResult", "HotelResultList"),
            ("HotelSearchResult", "Hotels"),
            ("data", "hotels"),
            ("response", "hotels"),
        ):
            node: Any = resp
            for key in path:
                node = node.get(key) if isinstance(node, dict) else None
                if node is None:
                    break
            if isinstance(node, list) and node:
                return node
        return []

    def _parse_response(self, resp: dict[str, Any], req: HotelSearchRequest) -> list[VendorHotel]:
        result: list[VendorHotel] = []
        for raw in self._extract_hotel_list(resp):
            if not isinstance(raw, dict):
                continue
            total = float(self._first(
                raw, "price", "Price", "total_price", "TotalPrice",
                "OfferedPrice", "PublishedPrice", default=0,
            ) or 0)
            if total <= 0:
                continue
            currency = str(self._first(raw, "currency", "Currency", "CurrencyCode",
                                       default=req.currency))
            stars_raw = self._first(raw, "star_rating", "StarRating", "starRating", default=None)
            try:
                stars = int(float(stars_raw)) if stars_raw is not None else None
            except (TypeError, ValueError):
                stars = None

            rating_raw = self._first(raw, "rating", "TripAdvisorRating", "guest_rating", default=None)
            try:
                rating = float(rating_raw) if rating_raw is not None else None
                if rating is not None and rating <= 5:
                    rating = round(rating * 2, 1)
            except (TypeError, ValueError):
                rating = None

            lat = self._first(raw, "latitude", "Latitude", "lat", default=None)
            lon = self._first(raw, "longitude", "Longitude", "lng", "lon", default=None)

            hotel = VendorHotel(
                vendor=self.name,
                vendor_hotel_id=str(self._first(raw, "hotel_code", "HotelCode", "hotelId",
                                                "ResultIndex", default="")),
                name=str(self._first(raw, "hotel_name", "HotelName", "name",
                                     default="Unknown Hotel")),
                address=str(self._first(raw, "address", "HotelAddress", "hotel_address",
                                        default="")),
                city=req.destination,
                country="",
                latitude=float(lat) if lat not in (None, "") else None,
                longitude=float(lon) if lon not in (None, "") else None,
                stars=stars,
                guest_rating=rating,
                amenities=[str(a) for a in (raw.get("amenities") or raw.get("HotelFacilities") or [])[:8]],
                images=[str(self._first(raw, "thumb_url", "HotelPicture", "image",
                                        default=""))] if self._first(
                    raw, "thumb_url", "HotelPicture", "image", default="") else [],
                description=str(self._first(raw, "description", "HotelDescription", default="")),
                rates=[RateDetail(
                    rate_id=str(self._first(raw, "token", "ResultIndex", "hotel_code", default="")),
                    room_type="Best Available",
                    meal_plan=str(self._first(raw, "boardType", "MealType", default="Room Only")),
                    price_per_night=round(total / req.nights, 2),
                    total_price=round(total, 2),
                    currency=currency,
                    is_refundable=not bool(raw.get("IsPackageRate", False)),
                )],
            )
            hotel.compute_pricing()
            result.append(hotel)
        return result
