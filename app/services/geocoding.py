"""
Geocoding Service — OSM Nominatim (free, real data, no API key).
Used by vendors that need lat/lng or country (HotelBeds, Travelomatix).
Results are cached in-memory for the process lifetime.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass

import httpx
import structlog

from app.core.config.settings import get_settings

logger = structlog.get_logger(__name__)


@dataclass(frozen=True)
class GeoLocation:
    latitude: float
    longitude: float
    display_name: str
    city: str
    country: str
    country_code: str   # ISO-2, upper


_cache: dict[str, GeoLocation] = {}
_lock = asyncio.Lock()


async def geocode(destination: str) -> GeoLocation:
    """Resolve a free-text destination to coordinates + country. Raises on failure."""
    key = destination.strip().lower()
    if key in _cache:
        return _cache[key]

    async with _lock:  # Nominatim usage policy: max 1 req/sec
        if key in _cache:
            return _cache[key]
        s = get_settings().geocoding
        async with httpx.AsyncClient(timeout=s.timeout) as client:
            resp = await client.get(
                f"{s.base_url}/search",
                params={
                    "q": destination,
                    "format": "jsonv2",
                    "limit": 1,
                    "addressdetails": 1,
                    "accept-language": "en",
                },
                headers={"User-Agent": s.user_agent},
            )
            resp.raise_for_status()
            data = resp.json()

        if not data:
            raise ValueError(f"Geocoding failed: no match for '{destination}'")

        top = data[0]
        addr = top.get("address", {})
        loc = GeoLocation(
            latitude=float(top["lat"]),
            longitude=float(top["lon"]),
            display_name=top.get("display_name", destination),
            city=addr.get("city") or addr.get("town") or addr.get("state") or destination,
            country=addr.get("country", ""),
            country_code=(addr.get("country_code") or "").upper(),
        )
        _cache[key] = loc
        logger.info("geocoded", destination=destination, lat=loc.latitude, lon=loc.longitude,
                    country=loc.country_code)
        return loc
