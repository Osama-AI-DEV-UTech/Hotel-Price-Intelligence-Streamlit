"""
Provider Registry — single source of truth for all vendor adapters.
Add a new vendor by appending its class here; everything else
(orchestrator, health, /vendors endpoint, UI) picks it up automatically.
"""
from __future__ import annotations

from app.providers.adapters.amadeus import AmadeusProvider
from app.providers.adapters.bookingcom import BookingComProvider
from app.providers.adapters.expedia import ExpediaProvider
from app.providers.adapters.hotelbeds import HotelBedsProvider
from app.providers.adapters.serpapi import SerpApiProvider
from app.providers.adapters.travelomatix import TravelomatixProvider
from app.providers.base.provider import BaseProvider

PROVIDER_CLASSES: list[type[BaseProvider]] = [
    SerpApiProvider,
    BookingComProvider,
    ExpediaProvider,
    HotelBedsProvider,
    AmadeusProvider,
    TravelomatixProvider,
]


def build_providers() -> list[BaseProvider]:
    providers = [cls() for cls in PROVIDER_CLASSES]
    providers.sort(key=lambda p: p.priority)
    return providers
