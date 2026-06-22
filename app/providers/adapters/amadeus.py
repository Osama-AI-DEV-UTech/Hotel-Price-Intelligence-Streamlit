"""
Amadeus Provider — OAuth2 client credentials → city lookup → hotel list → offers.
LIVE DATA ONLY. City codes are resolved via the live Amadeus Locations API
(no hardcoded city map).
Docs: https://developers.amadeus.com/self-service/category/hotels
"""
from __future__ import annotations

import re
import time
from typing import Any

import httpx
import structlog

from app.core.config.settings import get_settings
from app.providers.base.provider import BaseProvider, ProviderError
from app.schemas.models import HotelSearchRequest, RateDetail, VendorHotel

logger = structlog.get_logger(__name__)


class AmadeusProvider(BaseProvider):
    name = "amadeus"
    display_name = "Amadeus"
    priority = 3
    supports_timeline = False   # 2-step + batched offers — too slow for scans

    def __init__(self) -> None:
        super().__init__()
        s = get_settings().amadeus
        self._client_id = s.client_id
        self._client_secret = s.client_secret
        # Normalise: endpoints already carry their own /v1 /v3 prefixes, so a
        # trailing /vN in AMADEUS_BASE_URL (e.g. ".../v2") must be stripped.
        self._base_url = re.sub(r"/v\d+/?$", "", s.base_url.rstrip("/"))
        self._token: str | None = None
        self._token_expires: float = 0.0
        self._city_cache: dict[str, str] = {}

    def _create_client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            base_url=self._base_url,
            timeout=httpx.Timeout(connect=10.0, read=40.0, write=10.0, pool=5.0),
        )

    @property
    def configured(self) -> bool:
        return bool(self._client_id and self._client_secret)

    # ── Auth ───────────────────────────────────────────────────────────────────

    async def _get_token(self) -> str:
        if self._token and time.monotonic() < self._token_expires - 60:
            return self._token
        resp = await self.client.post(
            "/v1/security/oauth2/token",
            data={
                "grant_type": "client_credentials",
                "client_id": self._client_id,
                "client_secret": self._client_secret,
            },
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        if resp.status_code != 200:
            raise ProviderError(f"Amadeus auth failed: HTTP {resp.status_code} — {resp.text[:200]}")
        data = resp.json()
        self._token = data["access_token"]
        self._token_expires = time.monotonic() + data.get("expires_in", 1799)
        return self._token

    # ── Live city-code resolution ─────────────────────────────────────────────

    async def _city_code(self, destination: str, headers: dict[str, str]) -> str:
        key = destination.strip().lower()
        if key in self._city_cache:
            return self._city_cache[key]
        resp = await self._make_request(
            "GET", "/v1/reference-data/locations",
            params={"keyword": destination[:30], "subType": "CITY", "page[limit]": 5},
            headers=headers,
        )
        candidates = resp.get("data", [])
        if not candidates:
            raise ProviderError(f"Amadeus: no city match for '{destination}'")
        code = candidates[0].get("iataCode") or candidates[0].get("address", {}).get("cityCode")
        if not code:
            raise ProviderError(f"Amadeus: city '{destination}' has no IATA code")
        self._city_cache[key] = code
        return code

    # ── Search ─────────────────────────────────────────────────────────────────

    async def fetch(self, req: HotelSearchRequest) -> list[VendorHotel]:
        token = await self._get_token()
        headers = {"Authorization": f"Bearer {token}"}

        city_code = await self._city_code(req.destination, headers)

        hotel_resp = await self._make_request(
            "GET", "/v1/reference-data/locations/hotels/by-city",
            params={
                "cityCode": city_code,
                "radius": req.radius or 10,
                "radiusUnit": "KM",
                "hotelSource": "ALL",
            },
            headers=headers,
        )
        hotel_ids = [h["hotelId"] for h in hotel_resp.get("data", [])[:30]]
        if not hotel_ids:
            return []

        hotels: list[VendorHotel] = []
        for i in range(0, min(len(hotel_ids), 30), 10):   # max 10 IDs per offers call
            batch = hotel_ids[i:i + 10]
            try:
                offers_resp = await self._make_request(
                    "GET", "/v3/shopping/hotel-offers",
                    params={
                        "hotelIds": ",".join(batch),
                        "checkInDate": req.checkin.isoformat(),
                        "checkOutDate": req.checkout.isoformat(),
                        "adults": str(req.adults),
                        "roomQuantity": str(req.rooms),
                        "currency": req.currency,
                        "bestRateOnly": "false",
                        "paymentPolicy": "NONE",
                    },
                    headers=headers,
                )
                hotels.extend(self._parse_offers(offers_resp, hotel_resp, req))
            except Exception as batch_exc:  # partial batch failure is acceptable
                logger.warning("amadeus_batch_failed", batch=i, error=str(batch_exc))
                continue
        return hotels

    def _parse_offers(
        self,
        offers_resp: dict[str, Any],
        hotel_resp: dict[str, Any],
        req: HotelSearchRequest,
    ) -> list[VendorHotel]:
        hotel_info = {h["hotelId"]: h for h in hotel_resp.get("data", [])}
        result: list[VendorHotel] = []

        for item in offers_resp.get("data", []):
            h = item.get("hotel", {})
            hotel_id = h.get("hotelId", "")
            info = hotel_info.get(hotel_id, {})
            geo = h.get("geoCode") or info.get("geoCode") or {}
            addr_lines = h.get("address", {}).get("lines", [])

            vendor_hotel = VendorHotel(
                vendor=self.name,
                vendor_hotel_id=hotel_id,
                name=h.get("name") or info.get("name", "Unknown"),
                address=addr_lines[0] if addr_lines else "",
                city=h.get("cityCode", ""),
                country=h.get("countryCode") or info.get("address", {}).get("countryCode", ""),
                latitude=float(geo["latitude"]) if geo.get("latitude") else None,
                longitude=float(geo["longitude"]) if geo.get("longitude") else None,
                stars=int(h["rating"]) if str(h.get("rating", "")).isdigit() else None,
                amenities=[str(a) for a in info.get("amenities", [])[:8]],
            )

            for offer in item.get("offers", []):
                price = offer.get("price", {})
                total = float(price.get("total") or 0)
                if total <= 0:
                    continue
                room = offer.get("room", {})
                type_est = room.get("typeEstimated", {})
                vendor_hotel.rates.append(RateDetail(
                    rate_id=offer.get("id", ""),
                    room_type=type_est.get("category") or room.get("type", "Room"),
                    meal_plan=(offer.get("boardType") or "ROOM_ONLY").replace("_", " ").title(),
                    price_per_night=round(total / req.nights, 2),
                    total_price=round(total, 2),
                    currency=price.get("currency", req.currency),
                    is_refundable=offer.get("policies", {}).get("paymentType") != "GUARANTEE",
                ))

            vendor_hotel.compute_pricing()
            if vendor_hotel.rates:
                result.append(vendor_hotel)
        return result
