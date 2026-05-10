"""Async Qwen client over the DashScope OpenAI-compatible endpoint."""

from __future__ import annotations

import logging
import os
from typing import Final

import httpx
from tenacity import (
    AsyncRetrying,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from ..config import get_dashscope
from .prompts import build_messages
from .schema import ExtractionResult

LOGGER: Final[logging.Logger] = logging.getLogger(__name__)

DASHSCOPE_BASE_URL_DEFAULT: Final[str] = (
    "https://dashscope.aliyuncs.com/compatible-mode/v1"
)
DEFAULT_TIMEOUT_SECONDS: Final[float] = 30.0
RETRY_ATTEMPTS: Final[int] = 4
RETRY_WAIT_MAX_SECONDS: Final[float] = 20.0


class ExtractionError(RuntimeError):
    """Raised when extraction fails to produce a validated result."""


def _resolve_base_url() -> str:
    """Return the DashScope base URL, allowing env-var override.

    Strips surrounding whitespace and enclosing quotes so misconfigured
    .env files (``KEY="https://..."`` or trailing spaces) still produce a
    usable URL rather than failing later with an opaque transport error.
    """
    raw = os.getenv("DASHSCOPE_BASE_URL")
    if raw is None:
        return DASHSCOPE_BASE_URL_DEFAULT
    cleaned = raw.strip().strip('"').strip("'")
    return cleaned or DASHSCOPE_BASE_URL_DEFAULT


def _validate_base_url(url: str) -> str:
    """Ensure the URL has an explicit http(s) scheme; raise otherwise."""
    if not url.startswith(("http://", "https://")):
        raise ExtractionError(
            f"DashScope base URL is malformed: {url!r}. "
            f"Expected an absolute URL starting with 'https://'. "
            f"Check the DASHSCOPE_BASE_URL line in your .env."
        )
    return url


class QwenExtractor:
    """Async wrapper for Qwen via the DashScope chat-completions API.

    The class is reentrant as an async context manager. It owns its httpx
    client when one is not supplied externally, ensuring orderly shutdown.
    """

    def __init__(
        self,
        *,
        api_key: str | None = None,
        model: str | None = None,
        base_url: str | None = None,
        client: httpx.AsyncClient | None = None,
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
    ) -> None:
        settings = get_dashscope()
        self._api_key: str = api_key or settings.api_key
        self._model: str = model or settings.model
        self._base_url: str = _validate_base_url(base_url or _resolve_base_url())
        self._client: httpx.AsyncClient = client or httpx.AsyncClient(
            timeout=timeout_seconds,
        )
        self._owns_client: bool = client is None

    async def __aenter__(self) -> QwenExtractor:
        return self

    async def __aexit__(self, *_exc_info: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        """Close the underlying httpx client if owned by this instance."""
        if self._owns_client:
            await self._client.aclose()

    async def extract(self, description: str) -> ExtractionResult:
        """Extract structured signals from a single job description.

        Raises:
            ExtractionError: When the API does not return a JSON payload that
                satisfies the ``ExtractionResult`` schema after retries.
        """
        messages = build_messages(description)
        payload: str | None = None
        async for attempt in AsyncRetrying(
            stop=stop_after_attempt(RETRY_ATTEMPTS),
            wait=wait_exponential(multiplier=1.0, max=RETRY_WAIT_MAX_SECONDS),
            retry=retry_if_exception_type((httpx.HTTPError, httpx.TransportError)),
            reraise=True,
        ):
            with attempt:
                payload = await self._call_api(messages)
        if payload is None:
            raise ExtractionError("Empty response from DashScope.")
        try:
            return ExtractionResult.model_validate_json(payload)
        except Exception as exc:
            raise ExtractionError(
                f"Pydantic validation failed: {exc}. Payload preview: {payload[:300]!r}"
            ) from exc

    async def _call_api(self, messages: list[dict[str, str]]) -> str:
        response = await self._client.post(
            f"{self._base_url}/chat/completions",
            headers={"Authorization": f"Bearer {self._api_key}"},
            json={
                "model": self._model,
                "messages": messages,
                "response_format": {"type": "json_object"},
                "temperature": 0.0,
            },
        )
        if response.is_success:
            data = response.json()
            try:
                return data["choices"][0]["message"]["content"]
            except (KeyError, IndexError) as exc:
                raise ExtractionError(f"Unexpected API response shape: {data}") from exc

        # Non-success: surface the response body for diagnostics. Retryable
        # transport errors (429 / 5xx) raise httpx.HTTPStatusError so tenacity's
        # retry filter activates; everything else short-circuits as a fatal
        # ExtractionError with the same diagnostic body included.
        body_preview = response.text[:500]
        message = (
            f"DashScope HTTP {response.status_code} from model={self._model!r}: "
            f"{body_preview}"
        )
        if response.status_code in {429, 500, 502, 503, 504}:
            raise httpx.HTTPStatusError(
                message, request=response.request, response=response,
            )
        raise ExtractionError(message)
