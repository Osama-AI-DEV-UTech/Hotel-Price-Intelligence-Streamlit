"""
UbidStay Price Intelligence — Configuration
All settings come from environment variables / .env file. Nothing hardcoded.

IMPORTANT FIX: every nested settings class now declares env_file=".env".
Previously only the parent Settings read .env, so nested classes
(AmadeusSettings, HotelBedsSettings, ...) silently got empty keys and the
system fell back to demo data even though keys were present in .env.
"""
from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from dotenv import load_dotenv
from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

# Load .env into the actual process environment as early as possible so every
# nested BaseSettings class sees the values regardless of working directory.
_ENV_FILE = Path(__file__).resolve().parents[3] / ".env"
load_dotenv(_ENV_FILE, override=False)

_COMMON = dict(env_file=str(_ENV_FILE), env_file_encoding="utf-8", extra="ignore")


class OpenAISettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="OPENAI_", **_COMMON)
    api_key: str = Field(default="")
    model: str = Field(default="gpt-4o")
    max_tokens: int = Field(default=1500)
    temperature: float = Field(default=0.2)
    timeout: int = Field(default=60)


class HotelBedsSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="HOTELBEDS_", **_COMMON)
    api_key: str = Field(default="")
    api_secret: str = Field(default="")
    base_url: str = Field(default="https://api.test.hotelbeds.com")
    timeout: int = Field(default=30)


class AmadeusSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="AMADEUS_", **_COMMON)
    client_id: str = Field(default="")
    client_secret: str = Field(default="")
    base_url: str = Field(default="https://test.api.amadeus.com/v2")
    timeout: int = Field(default=30)


class SerpApiSettings(BaseSettings):
    """SerpAPI — Google Hotels engine. https://serpapi.com/google-hotels-api"""
    model_config = SettingsConfigDict(env_prefix="SERPAPI_", **_COMMON)
    api_key: str = Field(default="")
    base_url: str = Field(default="https://serpapi.com")
    timeout: int = Field(default=40)


class BookingComSettings(BaseSettings):
    """Booking.com via RapidAPI (booking-com15 by DataCrawler)."""
    model_config = SettingsConfigDict(env_prefix="BOOKINGCOM_", **_COMMON)
    rapidapi_key: str = Field(default="")          # falls back to RAPIDAPI_KEY
    rapidapi_host: str = Field(default="booking-com15.p.rapidapi.com")
    base_url: str = Field(default="https://booking-com15.p.rapidapi.com")
    timeout: int = Field(default=40)


class ExpediaSettings(BaseSettings):
    """Expedia / Hotels.com via RapidAPI (hotels-com-provider by tipsters)."""
    model_config = SettingsConfigDict(env_prefix="EXPEDIA_", **_COMMON)
    rapidapi_key: str = Field(default="")          # falls back to RAPIDAPI_KEY
    rapidapi_host: str = Field(default="hotels-com-provider.p.rapidapi.com")
    base_url: str = Field(default="https://hotels-com-provider.p.rapidapi.com")
    domain: str = Field(default="US")
    locale: str = Field(default="en_US")
    timeout: int = Field(default=40)


class TravelomatixSettings(BaseSettings):
    """
    Travelomatix B2B Hotel API (TravelNext v6 style).
    Credentials come from your Travelomatix account manager / dashboard.
    Every path is configurable because partner deployments differ.
    """
    model_config = SettingsConfigDict(env_prefix="TRAVELOMATIX_", **_COMMON)
    user_id: str = Field(default="")
    user_password: str = Field(default="")
    access: str = Field(default="Test")            # "Test" or "Production"
    base_url: str = Field(default="https://travelnext.works/api/hotel-api-v6")
    search_path: str = Field(default="/hotel_search")
    ip_address: str = Field(default="127.0.0.1")
    timeout: int = Field(default=45)


class GeocodingSettings(BaseSettings):
    """Free OSM Nominatim geocoder — no key required, real data."""
    model_config = SettingsConfigDict(env_prefix="GEOCODING_", **_COMMON)
    base_url: str = Field(default="https://nominatim.openstreetmap.org")
    user_agent: str = Field(default="UbidStay-PriceIntelligence/3.0 (ai.ubidtours@gmail.com)")
    timeout: int = Field(default=15)


class TimelineSettings(BaseSettings):
    """Future-date live price scan (price prediction engine)."""
    model_config = SettingsConfigDict(env_prefix="TIMELINE_", **_COMMON)
    offsets: str = Field(default="0,7,14,30,60,90")   # days ahead of requested checkin
    max_providers: int = Field(default=2)             # vendors used per scan point
    concurrency: int = Field(default=4)               # parallel scan calls
    enabled: bool = Field(default=True)


class HistorySettings(BaseSettings):
    """SQLite price-history store — every live search result is snapshotted."""
    model_config = SettingsConfigDict(env_prefix="HISTORY_", **_COMMON)
    enabled: bool = Field(default=True)
    db_path: str = Field(default="data/ubidstay.db")


class MonitorSettings(BaseSettings):
    """Watchlist auto-monitoring — background re-scans of saved searches."""
    model_config = SettingsConfigDict(env_prefix="MONITOR_", **_COMMON)
    enabled: bool = Field(default=True)
    interval_minutes: int = Field(default=720)        # 12h between automatic runs
    alert_drop_pct: float = Field(default=5.0)        # alert when price drops ≥ this %


class Settings(BaseSettings):
    model_config = SettingsConfigDict(case_sensitive=False, **_COMMON)

    app_name: str = Field(default="UbidStay Price Intelligence")
    app_version: str = Field(default="3.0.0")
    app_env: str = Field(default="development")
    debug: bool = Field(default=False)
    api_prefix: str = Field(default="/api/v1")
    cors_origins: list[str] = Field(default=["*"])

    # Shared RapidAPI key — used by any RapidAPI vendor without its own key
    rapidapi_key: str = Field(default="")

    openai: OpenAISettings = Field(default_factory=OpenAISettings)
    hotelbeds: HotelBedsSettings = Field(default_factory=HotelBedsSettings)
    amadeus: AmadeusSettings = Field(default_factory=AmadeusSettings)
    serpapi: SerpApiSettings = Field(default_factory=SerpApiSettings)
    bookingcom: BookingComSettings = Field(default_factory=BookingComSettings)
    expedia: ExpediaSettings = Field(default_factory=ExpediaSettings)
    travelomatix: TravelomatixSettings = Field(default_factory=TravelomatixSettings)
    geocoding: GeocodingSettings = Field(default_factory=GeocodingSettings)
    timeline: TimelineSettings = Field(default_factory=TimelineSettings)
    history: HistorySettings = Field(default_factory=HistorySettings)
    monitor: MonitorSettings = Field(default_factory=MonitorSettings)

    parallel_providers: bool = Field(default=True)

    @property
    def timeline_offsets(self) -> list[int]:
        out: list[int] = []
        for part in self.timeline.offsets.split(","):
            part = part.strip()
            if part.lstrip("-").isdigit():
                out.append(int(part))
        return sorted(set(out)) or [0, 30, 60, 90]


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
