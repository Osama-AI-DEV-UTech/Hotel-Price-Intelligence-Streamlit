"""
Price Intelligence Search Endpoint
POST /api/v1/hotels/search — vendor-by-vendor live results + comparison
+ live future-price timeline + AI recommendation.
"""
from __future__ import annotations

import structlog
from fastapi import APIRouter, HTTPException, status

from app.agents.orchestrator import get_orchestrator
from app.schemas.models import HotelSearchRequest, PriceComparisonResponse

logger = structlog.get_logger(__name__)
router = APIRouter()


@router.post(
    "/hotels/search",
    response_model=PriceComparisonResponse,
    status_code=status.HTTP_200_OK,
    summary="Hotel Price Intelligence Search (live data only)",
    description="""
Searches every configured vendor LIVE and returns:

**Per-vendor results** (`vendors`): each vendor's own hotel list with all rates.
A vendor without API keys is reported as `not_configured`; a failed call as
`api_error` with the real error message. There is no demo data.

**Cross-vendor comparison** (`comparisons`): the same property matched across
vendors — who is cheapest, who is most expensive, and the spread.

**Live price timeline** (`price_timeline`): the system re-queries the fastest
vendors at future date windows (+7/+14/+30/+60/+90 days) and builds a real
forward price curve — this is the prediction engine.

**AI recommendation** (`ai_recommendation`): BOOK_NOW / WAIT / MONITOR with
full analysis based exclusively on the live numbers above.

**Specific hotel mode**: pass `hotel_name` (e.g. "Grand Hotel & Spa") to
restrict the whole analysis to one property and see which vendor sells it
cheapest.
    """,
    responses={
        200: {"description": "Complete live price intelligence response"},
        422: {"description": "Invalid request parameters"},
        500: {"description": "Search failed"},
    },
)
async def search_hotels(request: HotelSearchRequest) -> PriceComparisonResponse:
    try:
        return await get_orchestrator().search(request)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc))
    except Exception as exc:
        logger.error("search_endpoint_failed", error=str(exc))
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Search failed: {exc}",
        )
