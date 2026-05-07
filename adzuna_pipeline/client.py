"""HTTP client for the Adzuna search API with retry and pagination support."""

from __future__ import annotations

import time
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Final

import httpx
from tenacity import (
    RetryCallState,
    Retrying,
    before_sleep_log,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

import logging

from .config import get_adzuna

LOGGER: Final[logging.Logger] = logging.getLogger(__name__)

ADZUNA_BASE_URL: Final[str] = "https://api.adzuna.com/v1/api/jobs"
DEFAULT_RESULTS_PER_PAGE: Final[int] = 50
DEFAULT_TIMEOUT_SECONDS: Final[float] = 30.0
DEFAULT_REQUEST_INTERVAL_SECONDS: Final[float] = 0.4
RETRY_ATTEMPTS: Final[int] = 6
RETRY_WAIT_MIN_SECONDS: Final[float] = 1.0
RETRY_WAIT_MAX_SECONDS: Final[float] = 30.0


class AdzunaApiError(RuntimeError):
    """Raised when the Adzuna API returns an unrecoverable error."""


def _is_retryable_status(exc: BaseException) -> bool:
    """Return True for HTTP responses that warrant a retry."""
    if isinstance(exc, httpx.HTTPStatusError):
        return exc.response.status_code in {429, 500, 502, 503, 504}
    return isinstance(exc, (httpx.TransportError, httpx.TimeoutException))


@dataclass(frozen=True)
class SearchQuery:
    """Parameters for a single Adzuna search request.

    Attributes:
        country: Adzuna country code (e.g. ``au``).
        where: Optional location filter; omitted when ``None``.
        category: Optional category slug; omitted when ``None``.
        max_days_old: Freshness window in days.
        results_per_page: Page size.
    """

    country: str
    where: str | None
    category: str | None
    max_days_old: int
    results_per_page: int = DEFAULT_RESULTS_PER_PAGE


@dataclass(frozen=True)
class SearchPage:
    """Single page of Adzuna search results.

    Attributes:
        page: 1-indexed page number.
        total_count: Total matching records across all pages.
        results: Job records returned on this page.
    """

    page: int
    total_count: int
    results: list[dict]


class AdzunaClient:
    """Adzuna search-API client with retries, rate limiting, and pagination.

    The client is intended to be used as a context manager so the underlying
    httpx connection pool is released cleanly.
    """

    def __init__(
        self,
        *,
        app_id: str | None = None,
        app_key: str | None = None,
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
        request_interval_seconds: float = DEFAULT_REQUEST_INTERVAL_SECONDS,
    ) -> None:
        settings = get_adzuna()
        self._app_id: str = app_id or settings.app_id
        self._app_key: str = app_key or settings.app_key
        self._request_interval: float = request_interval_seconds
        self._client: httpx.Client = httpx.Client(timeout=timeout_seconds)

    def __enter__(self) -> AdzunaClient:
        return self

    def __exit__(self, *_exc_info: object) -> None:
        self.close()

    def close(self) -> None:
        """Close the underlying HTTP connection pool."""
        self._client.close()

    def fetch_page(self, query: SearchQuery, page: int) -> SearchPage:
        """Fetch a single page with retry on transient failures.

        Args:
            query: Search parameters.
            page: 1-indexed page number.

        Returns:
            Parsed search page.

        Raises:
            AdzunaApiError: When the API returns a non-retryable error or
                exhausts the retry budget.
        """
        retrying = Retrying(
            stop=stop_after_attempt(RETRY_ATTEMPTS),
            wait=wait_exponential(
                multiplier=RETRY_WAIT_MIN_SECONDS,
                max=RETRY_WAIT_MAX_SECONDS,
            ),
            retry=retry_if_exception_type((httpx.HTTPError, httpx.TransportError)),
            before_sleep=before_sleep_log(LOGGER, logging.WARNING),
            reraise=True,
        )
        try:
            for attempt in retrying:
                with attempt:
                    payload = self._raw_get(query, page)
        except httpx.HTTPStatusError as exc:
            raise AdzunaApiError(
                f"Adzuna returned HTTP {exc.response.status_code} for page {page}: "
                f"{exc.response.text[:200]}"
            ) from exc
        except httpx.HTTPError as exc:
            raise AdzunaApiError(f"Adzuna transport error on page {page}: {exc}") from exc

        return SearchPage(
            page=page,
            total_count=int(payload.get("count", 0)),
            results=list(payload.get("results", [])),
        )

    def iter_pages(
        self,
        query: SearchQuery,
        *,
        max_pages: int,
        stop_when_empty: bool = True,
    ) -> Iterator[SearchPage]:
        """Iterate sequential search pages, yielding each as it arrives.

        Args:
            query: Search parameters.
            max_pages: Maximum number of pages to fetch.
            stop_when_empty: If True, stop when a page returns zero results.
        """
        for page in range(1, max_pages + 1):
            search_page = self.fetch_page(query, page)
            yield search_page
            if stop_when_empty and not search_page.results:
                return
            time.sleep(self._request_interval)

    def collect(
        self,
        query: SearchQuery,
        *,
        max_pages: int,
    ) -> tuple[int, list[dict]]:
        """Drain ``iter_pages`` into the (total_count, all_results) tuple.

        Args:
            query: Search parameters.
            max_pages: Maximum number of pages to fetch.

        Returns:
            A pair of the API-reported total count and the aggregated rows.
        """
        rows: list[dict] = []
        total_count = 0
        for page in self.iter_pages(query, max_pages=max_pages):
            if page.page == 1:
                total_count = page.total_count
            rows.extend(page.results)
        return total_count, rows

    def _raw_get(self, query: SearchQuery, page: int) -> dict:
        """Execute a single GET against the search endpoint."""
        url = f"{ADZUNA_BASE_URL}/{query.country}/search/{page}"
        params: dict[str, str | int] = {
            "app_id": self._app_id,
            "app_key": self._app_key,
            "results_per_page": query.results_per_page,
            "max_days_old": query.max_days_old,
            "content-type": "application/json",
        }
        if query.where:
            params["where"] = query.where
        if query.category:
            params["category"] = query.category

        response = self._client.get(url, params=params)
        response.raise_for_status()
        return response.json()


def _retry_log(retry_state: RetryCallState) -> None:  # pragma: no cover
    """Helper retained for future structured-logging hooks."""
    LOGGER.warning(
        "Adzuna retry attempt=%s outcome=%s",
        retry_state.attempt_number,
        retry_state.outcome,
    )
