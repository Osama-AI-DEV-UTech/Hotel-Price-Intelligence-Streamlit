"""
Expedia / Hotels.com Provider — via RapidAPI "hotels-com-provider" (tipsters).
Hotels.com is an Expedia Group brand; inventory and pricing come from Expedia.
Flow: /v2/regions (cached) → /v2/hotels/search.
Docs: https://rapidapi.com/tipsters/api/hotels-com-provider
"""
from __future__ import annotations

from typing import Any

import httpx
import structlog

from app.core.config.settings import get_settings
from app.providers.base.provider import BaseProvider, ProviderError
from app.schemas.models import HotelSearchRequest, RateDetail, VendorHotel

logger = structlog.get_logger(__name__)


class ExpediaProvider(BaseProvider):
    name = "expedia"
    display_name = "Expedia / Hotels.com (RapidAPI)"
    priority = 2
    supports_timeline = True

    def __init__(self) -> None:
        super().__init__()
        s = get_settings()
        self._api_key = s.expedia.rapidapi_key or s.rapidapi_key
        self._host = s.expedia.rapidapi_host
        self._base_url = s.expedia.base_url
        self._domain = s.expedia.domain
        self._locale = s.expedia.locale
        self._region_cache: dict[str, str] = {}

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

    async def _resolve_region(self, destination: str) -> str:
        key = destination.strip().lower()
        if key in self._region_cache:
            return self._region_cache[key]
        resp = await self._make_request(
            "GET", "/v2/regions",
            params={"query": destination, "domain": self._domain, "locale": self._locale},
            headers=self._headers(),
        )
        candidates = resp.get("data") or []
        region_id = ""
        for c in candidates:
            gaia = c.get("gaiaId") or (c.get("essId") or {}).get("sourceId")
            if gaia:
                region_id = str(gaia)
                break
        if not region_id:
            raise ProviderError(f"Expedia: no region match for '{destination}'")
        self._region_cache[key] = region_id
        return region_id

    async def fetch(self, req: HotelSearchRequest) -> list[VendorHotel]:
        region_id = await self._resolve_region(req.destination)

        params: dict[str, Any] = {
            "region_id": region_id,
            "checkin_date": req.checkin.isoformat(),
            "checkout_date": req.checkout.isoformat(),
            "adults_number": req.adults,
            "domain": self._domain,
            "locale": self._locale,
            "sort_order": "RECOMMENDED",
            "available_filter": "SHOW_AVAILABLE_ONLY",
            "page_number": 1,
        }
        if req.children:
            params["children_ages"] = ",".join(str(a) for a in req.resolved_children_ages)
        if req.budget:
            params["price_max"] = int(req.budget)
        if req.stars and 1 <= req.stars <= 5:
            params["star_rating_ids"] = ",".join(str(s) for s in range(req.stars, 6))

        resp = await self._make_request(
            "GET", "/v2/hotels/search",
            params=params,
            headers=self._headers(),
        )
        return self._parse_response(resp, req)

    def _parse_response(self, resp: dict[str, Any], req: HotelSearchRequest) -> list[VendorHotel]:
        # Handle both response shapes the API has shipped
        raw_list = (
            resp.get("properties")
            or (resp.get("data") or {}).get("propertySearch", {}).get("properties")
            or []
        )
        result: list[VendorHotel] = []
        for raw in raw_list:
            price = raw.get("price") or {}
            lead = price.get("lead") or {}
            per_night = float(lead.get("amount") or 0)
            if per_night <= 0:
                continue
            currency = (lead.get("currencyInfo") or {}).get("code") or req.currency

            map_marker = raw.get("mapMarker") or {}
            lat_long = map_marker.get("latLong") or {}
            reviews = raw.get("reviews") or {}
            neighborhood = raw.get("neighborhood") or {}
            prop_image = ((raw.get("propertyImage") or {}).get("image") or {})

            hotel = VendorHotel(
                vendor=self.name,
                vendor_hotel_id=str(raw.get("id", "")),
                name=raw.get("name", "Unknown Hotel"),
                address=neighborhood.get("name", ""),
                city=req.destination,
                country="",
                latitude=lat_long.get("latitude"),
                longitude=lat_long.get("longitude"),
                stars=int(raw["star"]) if raw.get("star") else None,
                guest_rating=float(reviews["score"]) if reviews.get("score") else None,
                rating_count=reviews.get("total"),
                images=[prop_image.get("url", "")] if prop_image.get("url") else [],
                rates=[RateDetail(
                    rate_id=str(raw.get("id", "")),
                    room_type="Best Available",
                    meal_plan="Room Only",
                    price_per_night=round(per_night, 2),
                    total_price=round(per_night * req.nights, 2),
                    currency=currency,
                )],
            )

            # strikeOut = real published higher rate
            strike = price.get("strikeOut") or {}
            strike_val = float(strike.get("amount") or 0)
            if strike_val > per_night:
                hotel.rates.append(RateDetail(
                    rate_id=f"{raw.get('id', '')}-published",
                    room_type="Published Rate",
                    meal_plan="Room Only",
                    price_per_night=round(strike_val, 2),
                    total_price=round(strike_val * req.nights, 2),
                    currency=currency,
                ))

            hotel.compute_pricing()
            result.append(hotel)
        return result
