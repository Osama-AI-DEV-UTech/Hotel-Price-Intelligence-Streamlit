"""
Hotel Matching Engine
Matches the same physical hotel across different vendor results.
Uses name similarity + geographic proximity.
"""
from __future__ import annotations

import re

from app.schemas.models import HotelComparison, VendorHotel, VendorPriceSummary


def _normalize_name(name: str) -> str:
    """Normalize hotel name for comparison."""
    name = name.lower()
    # Remove common suffixes/prefixes that vary between vendors
    removals = [
        "hotel", "resort", "suites", "inn", "&", "and", "the",
        "by", "a", "an", "luxury", "boutique", "-", ",", ".",
    ]
    for word in removals:
        name = re.sub(r"\b" + re.escape(word) + r"\b", " ", name)
    return re.sub(r"\s+", " ", name).strip()


def _name_similarity(a: str, b: str) -> float:
    """Simple character-level similarity score 0–1."""
    a, b = _normalize_name(a), _normalize_name(b)
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0
    # Check if one contains the other
    if a in b or b in a:
        return 0.85
    # Word overlap
    words_a = set(a.split())
    words_b = set(b.split())
    if not words_a or not words_b:
        return 0.0
    intersection = words_a & words_b
    union = words_a | words_b
    return len(intersection) / len(union)


def _geo_distance_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Haversine distance in km."""
    import math
    R = 6371
    d_lat = math.radians(lat2 - lat1)
    d_lon = math.radians(lon2 - lon1)
    a = math.sin(d_lat / 2) ** 2 + math.cos(math.radians(lat1)) * math.cos(math.radians(lat2)) * math.sin(d_lon / 2) ** 2
    return R * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))


def match_hotels_across_vendors(
    all_hotels: list[VendorHotel],
    name_threshold: float = 0.65,
    geo_threshold_km: float = 0.3,
) -> list[HotelComparison]:
    """
    Find hotels that appear in multiple vendors and build comparison objects.
    Returns list of HotelComparison sorted by price_difference (biggest savings first).
    """
    comparisons: list[HotelComparison] = []
    used: set[str] = set()  # "vendor:hotel_id"

    for i, hotel_a in enumerate(all_hotels):
        key_a = f"{hotel_a.vendor}:{hotel_a.vendor_hotel_id}"
        if key_a in used or hotel_a.lowest_rate is None:
            continue

        matched_vendors: list[VendorHotel] = [hotel_a]

        for j, hotel_b in enumerate(all_hotels):
            if i == j:
                continue
            if hotel_b.vendor == hotel_a.vendor:
                continue  # same vendor
            if hotel_b.lowest_rate is None:
                continue
            key_b = f"{hotel_b.vendor}:{hotel_b.vendor_hotel_id}"
            if key_b in used:
                continue

            # Match by name
            name_score = _name_similarity(hotel_a.name, hotel_b.name)
            matched = name_score >= name_threshold

            # Also match by proximity if coords available
            if (
                not matched
                and hotel_a.latitude and hotel_a.longitude
                and hotel_b.latitude and hotel_b.longitude
            ):
                dist = _geo_distance_km(
                    hotel_a.latitude, hotel_a.longitude,
                    hotel_b.latitude, hotel_b.longitude,
                )
                if dist <= geo_threshold_km and name_score >= 0.4:
                    matched = True

            if matched:
                matched_vendors.append(hotel_b)

        if len(matched_vendors) < 2:
            # Still include single-vendor hotels for completeness
            used.add(key_a)
            if hotel_a.lowest_rate:
                comp = HotelComparison(
                    canonical_name=hotel_a.name,
                    canonical_address=hotel_a.address,
                    stars=hotel_a.stars,
                    guest_rating=hotel_a.guest_rating,
                    latitude=hotel_a.latitude,
                    longitude=hotel_a.longitude,
                    vendor_prices=[
                        VendorPriceSummary(
                            vendor=hotel_a.vendor,
                            vendor_hotel_id=hotel_a.vendor_hotel_id,
                            price_per_night=hotel_a.lowest_rate,
                            total_price=min((r.total_price for r in hotel_a.rates if r.available), default=0),
                            currency="USD",
                            room_type=hotel_a.best_room_type,
                            is_refundable=any(r.is_refundable for r in hotel_a.rates),
                            rate_id=hotel_a.rates[0].rate_id if hotel_a.rates else "",
                        )
                    ],
                )
                comp.compute_comparison()
                comparisons.append(comp)
            continue

        # Mark all as used
        for h in matched_vendors:
            used.add(f"{h.vendor}:{h.vendor_hotel_id}")

        # Build comparison with best price from each vendor
        vendor_prices: list[VendorPriceSummary] = []
        for h in matched_vendors:
            best_rate = min(
                (r for r in h.rates if r.available),
                key=lambda r: r.price_per_night,
                default=None,
            )
            if best_rate:
                vendor_prices.append(VendorPriceSummary(
                    vendor=h.vendor,
                    vendor_hotel_id=h.vendor_hotel_id,
                    price_per_night=best_rate.price_per_night,
                    total_price=best_rate.total_price,
                    currency=best_rate.currency,
                    room_type=best_rate.room_type,
                    is_refundable=best_rate.is_refundable,
                    rate_id=best_rate.rate_id,
                ))

        # Use the hotel with most info as canonical
        canonical = max(matched_vendors, key=lambda h: len(h.amenities) + (1 if h.guest_rating else 0))
        comp = HotelComparison(
            canonical_name=canonical.name,
            canonical_address=canonical.address,
            stars=canonical.stars,
            guest_rating=canonical.guest_rating,
            latitude=canonical.latitude,
            longitude=canonical.longitude,
            vendor_prices=vendor_prices,
        )
        comp.compute_comparison()
        comparisons.append(comp)

    # Sort: matched hotels first (most vendors), then by price_difference descending
    comparisons.sort(key=lambda c: (-len(c.vendor_prices), -c.price_difference))
    return comparisons
