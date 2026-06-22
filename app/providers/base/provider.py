"""
Base Provider — shared HTTP client, retries, circuit breaker.

LIVE DATA CONTRACT (no exceptions):
  - credentials missing  → ([], "not_configured", message)   — vendor skipped
  - live call succeeds   → (hotels, "success", "")
  - live call empty      → ([], "no_results", "")
  - live call fails      → ([], "api_error", error message)
There is NO demo/mock fallback anywhere in this codebase.
"""
from __future__ import annotations

import asyncio
import time
from abc import ABC, abstractmethod
from typing import Any

import httpx
import structlog

from app.schemas.models import (
    STATUS_API_ERROR,
    STATUS_NO_RESULTS,
    STATUS_NOT_CONFIGURED,
    STATUS_SUCCESS,
    HotelSearchRequest,
    VendorHotel,
)

logger = structlog.get_logger(__name__)


class ProviderError(Exception):
    """Raised by adapters when a vendor API returns an unusable response."""


class BaseProvider(ABC):
    name: str = "base"
    display_name: str = "Base Provider"
    priority: int = 99
    supports_timeline: bool = True   # cheap enough to use in future-date scans

    def __init__(self) -> None:
        self._client: httpx.AsyncClient | None = None
        self._failure_count = 0
        self._circuit_open = False
        self._circuit_open_at: float = 0.0

    # ── Abstract API ───────────────────────────────────────────────────────────

    @property
    @abstractmethod
    def configured(self) -> bool:
        """True when all required credentials are present."""

    @abstractmethod
    async def fetch(self, req: HotelSearchRequest) -> list[VendorHotel]:
        """Call the live vendor API and return parsed hotels. May raise."""

    # ── Public entrypoint ──────────────────────────────────────────────────────

    async def search(self, req: HotelSearchRequest) -> tuple[list[VendorHotel], str, str]:
        """Returns (hotels, status, error_message). Never returns fake data."""
        if not self.configured:
            return [], STATUS_NOT_CONFIGURED, f"{self.display_name}: API credentials not configured"
        try:
            hotels = await self.fetch(req)
        except Exception as exc:  # noqa: BLE001 — surface every vendor error
            logger.error("vendor_api_error", vendor=self.name, error=str(exc))
            return [], STATUS_API_ERROR, str(exc)
        if not hotels:
            return [], STATUS_NO_RESULTS, ""
        logger.info("vendor_success", vendor=self.name, count=len(hotels))
        return hotels, STATUS_SUCCESS, ""

    # ── HTTP plumbing ──────────────────────────────────────────────────────────

    @property
    def client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = self._create_client()
        return self._client

    def _create_client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            timeout=httpx.Timeout(connect=10.0, read=40.0, write=10.0, pool=5.0),
            limits=httpx.Limits(max_connections=20, max_keepalive_connections=10),
            follow_redirects=True,
        )

    async def close(self) -> None:
        if self._client and not self._client.is_closed:
            await self._client.aclose()

    def _is_circuit_open(self) -> bool:
        """Open after 5 consecutive failures, auto-reset after 60s."""
        if self._circuit_open:
            if time.monotonic() - self._circuit_open_at > 60:
                self._circuit_open = False
                self._failure_count = 0
                logger.info("circuit_breaker_reset", provider=self.name)
            else:
                return True
        return False

    def _record_failure(self) -> None:
        self._failure_count += 1
        if self._failure_count >= 5:
            self._circuit_open = True
            self._circuit_open_at = time.monotonic()
            logger.warning("circuit_breaker_opened", provider=self.name)

    def _record_success(self) -> None:
        self._failure_count = max(0, self._failure_count - 1)

    async def _make_request(
        self,
        method: str,
        url: str,
        max_retries: int = 2,
        **kwargs: Any,
    ) -> dict[str, Any]:
        """HTTP request with retry, backoff and circuit breaker."""
        if self._is_circuit_open():
            raise ProviderError(f"{self.display_name}: circuit breaker open (too many recent failures)")

        last_exc: Exception = ProviderError("No attempts made")
        for attempt in range(max_retries + 1):
            try:
                resp = await self.client.request(method, url, **kwargs)
                resp.raise_for_status()
                self._record_success()
                return resp.json()
            except httpx.HTTPStatusError as exc:
                body = exc.response.text[:300]
                last_exc = ProviderError(
                    f"{self.display_name}: HTTP {exc.response.status_code} — {body}"
                )
                if exc.response.status_code in (400, 401, 403, 404, 422):
                    self._record_failure()
                    raise last_exc  # not retryable
                if exc.response.status_code == 429:
                    await asyncio.sleep(2 ** attempt)
                self._record_failure()
            except (httpx.TimeoutException, httpx.ConnectError) as exc:
                last_exc = ProviderError(f"{self.display_name}: {type(exc).__name__} — {exc}")
                self._record_failure()
                if attempt < max_retries:
                    await asyncio.sleep(0.5 * (attempt + 1))

        raise last_exc
