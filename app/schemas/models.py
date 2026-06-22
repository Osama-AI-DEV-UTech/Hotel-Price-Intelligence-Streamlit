"""
UbidStay Schemas — request/response models.
Design: each vendor's hotels are returned separately, then cross-vendor
comparison + live price timeline + AI recommendation are layered on top.
Every number in every model comes from a live vendor API. No demo data.
"""
from __future__ import annotations

from datetime import date, datetime
from typing import Any

from pydantic import BaseModel, Field, model_validator

# ── Vendor search statuses ─────────────────────────────────────────────────────
STATUS_SUCCESS = "success"                # live API data returned
STATUS_NOT_CONFIGURED = "not_configured"  # API key missing — vendor skipped
STATUS_API_ERROR = "api_error"            # live call failed (error included)
STATUS_NO_RESULTS = "no_results"          # live call OK but zero hotels


# ── Request ────────────────────────────────────────────────────────────────────

class HotelSearchRequest(BaseModel):
    destination: str = Field(..., min_length=2, description="City, ZIP, landmark, or address")
    checkin: date
    checkout: date
    rooms: int = Field(default=1, ge=1, le=10)
    adults: int = Field(default=2, ge=1, le=20)
    children: int = Field(default=0, ge=0, le=10)
    children_ages: list[int] = Field(default_factory=list, description="Ages for each child (optional)")
    budget: float = Field(default=0.0, ge=0, description="Max price per night (0 = no limit)")
    stars: int = Field(default=0, ge=0, le=7, description="Min stars (0 = any)")
    rating: float = Field(default=0.0, ge=0, le=10, description="Min guest rating (0 = any)")
    radius: int = Field(default=10, ge=1, le=100, description="Search radius in KM")
    accommodation_type: str = Field(default="hotel")
    currency: str = Field(default="USD", min_length=3, max_length=3)

    # Optional — analyse one specific hotel across vendors
    hotel_name: str | None = Field(
        default=None,
        description="Optional. e.g. 'Grand Hotel & Spa' — restrict analysis to this property",
    )

    # Optional — disable the future-date scan for faster responses
    include_timeline: bool = Field(default=True)

    @model_validator(mode="after")
    def checkout_after_checkin(self) -> "HotelSearchRequest":
        if self.checkout <= self.checkin:
            raise ValueError("checkout must be after checkin")
        return self

    @property
    def nights(self) -> int:
        return max(1, (self.checkout - self.checkin).days)

    @property
    def resolved_children_ages(self) -> list[int]:
        """Ages list padded/truncated to children count (default age 8)."""
        ages = list(self.children_ages)[: self.children]
        while len(ages) < self.children:
            ages.append(8)
        return ages


# ── Per-Vendor Hotel + Rate ────────────────────────────────────────────────────

class RateDetail(BaseModel):
    """Single room rate from a vendor — straight from the live API."""
    rate_id: str = ""
    room_type: str = ""
    meal_plan: str = "Room Only"
    price_per_night: float
    total_price: float
    currency: str = "USD"
    is_refundable: bool = True
    cancellation_deadline: str = ""
    available: bool = True


class VendorHotel(BaseModel):
    """A hotel as returned by one specific vendor, with all its rates."""
    vendor: str
    vendor_hotel_id: str

    name: str
    address: str = ""
    city: str = ""
    country: str = ""
    latitude: float | None = None
    longitude: float | None = None
    stars: int | None = None
    guest_rating: float | None = None        # normalised to 0–10
    rating_count: int | None = None
    amenities: list[str] = Field(default_factory=list)
    images: list[str] = Field(default_factory=list)
    description: str = ""

    rates: list[RateDetail] = Field(default_factory=list)

    lowest_rate: float | None = None
    highest_rate: float | None = None
    best_room_type: str = ""
    best_meal_plan: str = ""

    def compute_pricing(self) -> None:
        available = [r for r in self.rates if r.available]
        if available:
            prices = [r.price_per_night for r in available]
            self.lowest_rate = round(min(prices), 2)
            self.highest_rate = round(max(prices), 2)
            best = min(available, key=lambda r: r.price_per_night)
            self.best_room_type = best.room_type
            self.best_meal_plan = best.meal_plan


# ── Cross-Vendor Comparison ────────────────────────────────────────────────────

class VendorPriceSummary(BaseModel):
    vendor: str
    vendor_hotel_id: str
    price_per_night: float
    total_price: float
    currency: str = "USD"
    room_type: str = ""
    is_refundable: bool = True
    rate_id: str = ""


class HotelComparison(BaseModel):
    """Same physical hotel matched across vendors — who is cheapest."""
    canonical_name: str
    canonical_address: str = ""
    stars: int | None = None
    guest_rating: float | None = None
    latitude: float | None = None
    longitude: float | None = None

    vendor_prices: list[VendorPriceSummary] = Field(default_factory=list)

    cheapest_vendor: str = ""
    cheapest_price: float = 0.0
    most_expensive_vendor: str = ""
    most_expensive_price: float = 0.0
    price_difference: float = 0.0
    price_difference_pct: float = 0.0
    recommended_vendor: str = ""
    savings_if_best: float = 0.0

    def compute_comparison(self) -> None:
        if not self.vendor_prices:
            return
        sorted_prices = sorted(self.vendor_prices, key=lambda x: x.price_per_night)
        cheapest, expensive = sorted_prices[0], sorted_prices[-1]
        self.cheapest_vendor = cheapest.vendor
        self.cheapest_price = cheapest.price_per_night
        self.most_expensive_vendor = expensive.vendor
        self.most_expensive_price = expensive.price_per_night
        self.price_difference = round(self.most_expensive_price - self.cheapest_price, 2)
        if self.cheapest_price > 0:
            self.price_difference_pct = round(self.price_difference / self.cheapest_price * 100, 1)
        self.recommended_vendor = cheapest.vendor
        self.savings_if_best = self.price_difference


# ── Live Price Timeline (prediction engine output) ────────────────────────────

class TimelinePoint(BaseModel):
    """One live scan of the market at a (future) date window."""
    label: str                       # e.g. "+30 days"
    offset_days: int
    checkin: str
    checkout: str
    min_price: float = 0.0
    avg_price: float = 0.0
    median_price: float = 0.0
    max_price: float = 0.0
    sample_size: int = 0             # number of live hotel prices in this point
    vendors_used: list[str] = Field(default_factory=list)
    status: str = STATUS_SUCCESS     # success | api_error | no_results


class PriceTimeline(BaseModel):
    """
    Real forward price curve built by LIVE-querying vendors at multiple
    future date windows. This replaces any static seasonal profile.
    """
    destination: str
    currency: str = "USD"
    stay_nights: int = 1
    points: list[TimelinePoint] = Field(default_factory=list)

    trend: str = "stable"            # rising | falling | stable
    trend_pct: float = 0.0           # avg future price vs requested window, %
    current_window_avg: float = 0.0  # avg at requested dates
    cheapest_window: str = ""        # label of cheapest scanned window
    cheapest_window_avg: float = 0.0
    cheapest_window_checkin: str = ""
    most_expensive_window: str = ""
    most_expensive_window_avg: float = 0.0
    potential_saving_pct: float = 0.0  # requested window vs cheapest window
    best_booking_advice: str = ""
    vendors_used: list[str] = Field(default_factory=list)
    confidence: float = 0.0          # based on sample sizes + vendor count


# ── AI Recommendation ──────────────────────────────────────────────────────────

class AIRecommendation(BaseModel):
    action: str                      # BOOK_NOW | WAIT | MONITOR
    urgency: str                     # high | medium | low
    confidence: float
    headline: str
    full_analysis: str
    best_vendor: str
    best_price: float
    potential_saving: float
    best_time_to_book: str
    avoid_periods: list[str] = Field(default_factory=list)
    tips: list[str] = Field(default_factory=list)
    engine: str = "deterministic"    # "openai" | "deterministic"


# ── Agent Orchestration Trace ─────────────────────────────────────────────────

class AgentStep(BaseModel):
    agent: str
    status: str = "completed"        # completed | failed | skipped
    duration_ms: int = 0
    detail: str = ""


# ── Main Response ──────────────────────────────────────────────────────────────

class VendorResultSet(BaseModel):
    vendor: str
    vendor_display_name: str
    hotels_found: int
    hotels: list[VendorHotel] = Field(default_factory=list)
    search_status: str = STATUS_SUCCESS
    response_time_ms: int = 0
    error: str = ""


class PriceComparisonResponse(BaseModel):
    search_id: str
    destination: str
    hotel_name: str | None = None
    checkin: str
    checkout: str
    nights: int
    adults: int
    rooms: int
    currency: str = "USD"
    searched_at: datetime = Field(default_factory=datetime.utcnow)
    total_search_time_ms: int = 0

    vendors: list[VendorResultSet] = Field(default_factory=list)
    total_hotels_found: int = 0

    comparisons: list[HotelComparison] = Field(default_factory=list)

    market_lowest_price: float = 0.0
    market_highest_price: float = 0.0
    market_average_price: float = 0.0
    cheapest_vendor_overall: str = ""
    cheapest_hotel_overall: str = ""
    most_expensive_vendor_overall: str = ""

    price_timeline: PriceTimeline | None = None
    ai_recommendation: AIRecommendation | None = None

    agent_trace: list[AgentStep] = Field(default_factory=list)
    summary: dict[str, Any] = Field(default_factory=dict)


# ── Vendor status (GET /vendors) ──────────────────────────────────────────────

class VendorStatus(BaseModel):
    name: str
    display_name: str
    configured: bool
    priority: int
    supports_timeline: bool = True
    note: str = ""
