"""LLM-as-judge framework for evaluating extraction quality.

A stronger Qwen variant scores the extraction output of the production
extractor on a per-field basis. Each field receives a verdict
(correct / incorrect / uncertain) and a one-sentence justification.
"""

from __future__ import annotations

import json
import logging
import os
from enum import Enum
from typing import Final

import httpx
from pydantic import BaseModel, ConfigDict
from tenacity import (
    AsyncRetrying,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from ..config import get_dashscope
from .client import (
    DASHSCOPE_BASE_URL_DEFAULT,
    DEFAULT_TIMEOUT_SECONDS,
    RETRY_ATTEMPTS,
    RETRY_WAIT_MAX_SECONDS,
    ExtractionError,
    _resolve_base_url,
    _validate_base_url,
)
from .schema import ExtractionResult

LOGGER: Final[logging.Logger] = logging.getLogger(__name__)

DEFAULT_JUDGE_MODEL: Final[str] = "qwen-plus"
JUDGED_FIELDS: Final[tuple[str, ...]] = (
    "required_skills",
    "years_experience",
    "sponsorship_signal",
    "local_experience_required",
    "remote_friendly",
)


class Verdict(str, Enum):
    """Outcome of a single field judgment."""

    CORRECT = "correct"
    INCORRECT = "incorrect"
    UNCERTAIN = "uncertain"


class FieldJudgment(BaseModel):
    """Per-field verdict produced by the judge."""

    model_config = ConfigDict(extra="ignore")

    verdict: Verdict
    reasoning: str


class ExtractionJudgment(BaseModel):
    """Aggregate judgment over the five extracted fields."""

    model_config = ConfigDict(extra="ignore")

    required_skills: FieldJudgment
    years_experience: FieldJudgment
    sponsorship_signal: FieldJudgment
    local_experience_required: FieldJudgment
    remote_friendly: FieldJudgment

    def per_field(self) -> dict[str, Verdict]:
        return {field: getattr(self, field).verdict for field in JUDGED_FIELDS}


_JUDGE_SYSTEM_PROMPT: Final[str] = (
    "You evaluate structured extractions produced by a smaller model from "
    "Australian job postings. For each of five fields, decide whether the "
    "extracted value faithfully represents the source description.\n"
    "\n"
    "Mark 'correct' when the value matches what the description says, "
    "including the case where the description is silent and the extractor "
    "returned the default (unspecified / null / false).\n"
    "\n"
    "Mark 'incorrect' when the extractor:\n"
    "  - omits information that is clearly stated in the description,\n"
    "  - invents information that is not in the description, or\n"
    "  - uses the wrong enum / type.\n"
    "\n"
    "Mark 'uncertain' only when the description is genuinely ambiguous.\n"
    "\n"
    "Conservative principle: if the description does not mention a field, "
    "the extractor must return the default. Returning a default in that "
    "case is correct.\n"
    "\n"
    "Output JSON with this exact structure, no preamble or commentary:\n"
    "{\n"
    '  "required_skills":            {"verdict": "<v>", "reasoning": "<one sentence>"},\n'
    '  "years_experience":           {"verdict": "<v>", "reasoning": "<one sentence>"},\n'
    '  "sponsorship_signal":         {"verdict": "<v>", "reasoning": "<one sentence>"},\n'
    '  "local_experience_required":  {"verdict": "<v>", "reasoning": "<one sentence>"},\n'
    '  "remote_friendly":            {"verdict": "<v>", "reasoning": "<one sentence>"}\n'
    "}\n"
    'Where <v> is one of "correct", "incorrect", "uncertain".'
)


def build_judge_messages(
    description: str,
    extraction: ExtractionResult,
) -> list[dict[str, str]]:
    """Construct the judge's chat messages array."""
    extraction_view = {
        "required_skills": extraction.required_skills,
        "years_experience": extraction.years_experience,
        "sponsorship_signal": extraction.sponsorship_signal.value,
        "local_experience_required": extraction.local_experience_required,
        "remote_friendly": extraction.remote_friendly.value,
    }
    user_content = (
        f"Description:\n{description}\n\n"
        f"Extraction:\n{json.dumps(extraction_view, ensure_ascii=False, indent=2)}\n\n"
        "Return the per-field judgment JSON now."
    )
    return [
        {"role": "system", "content": _JUDGE_SYSTEM_PROMPT},
        {"role": "user", "content": user_content},
    ]


def _resolve_judge_model() -> str:
    """Return the judge model name from env override or built-in default."""
    return os.getenv("JUDGE_MODEL") or DEFAULT_JUDGE_MODEL


class QwenJudge:
    """Async judge wrapper that evaluates extractions against descriptions."""

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
        self._model: str = model or _resolve_judge_model()
        self._base_url: str = _validate_base_url(
            base_url or _resolve_base_url() or DASHSCOPE_BASE_URL_DEFAULT
        )
        self._client: httpx.AsyncClient = client or httpx.AsyncClient(
            timeout=timeout_seconds,
        )
        self._owns_client: bool = client is None

    @property
    def model(self) -> str:
        return self._model

    async def __aenter__(self) -> QwenJudge:
        return self

    async def __aexit__(self, *_exc_info: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def judge(
        self,
        description: str,
        extraction: ExtractionResult,
    ) -> ExtractionJudgment:
        """Judge a single extraction against its description.

        Raises:
            ExtractionError: When the API does not return a valid judgment.
        """
        messages = build_judge_messages(description, extraction)
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
            raise ExtractionError("Empty response from judge.")
        try:
            return ExtractionJudgment.model_validate_json(payload)
        except Exception as exc:
            raise ExtractionError(
                f"Judge response failed validation: {exc}. Payload: {payload[:300]!r}"
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
                raise ExtractionError(f"Unexpected judge response shape: {data}") from exc

        body_preview = response.text[:500]
        message = (
            f"Judge HTTP {response.status_code} from model={self._model!r}: {body_preview}"
        )
        if response.status_code in {429, 500, 502, 503, 504}:
            raise httpx.HTTPStatusError(
                message, request=response.request, response=response,
            )
        raise ExtractionError(message)
