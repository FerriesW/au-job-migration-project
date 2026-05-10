"""Pydantic models for the LLM extraction output contract."""

from __future__ import annotations

from enum import Enum
from typing import Any, Final

from pydantic import BaseModel, ConfigDict, Field, field_validator

# Synonym maps for the enum fields. The system prompt directs Qwen to use the
# canonical enum values, but the model occasionally emits paraphrases such as
# "fully remote" or "yes". We normalise those at the validation boundary so the
# stricter contract is preserved without losing usable extractions.

_REMOTE_ALIASES: Final[dict[str, str]] = {
    "fully remote": "remote",
    "full remote": "remote",
    "fully-remote": "remote",
    "fully_remote": "remote",
    "100% remote": "remote",
    "wfh": "remote",
    "work from home": "remote",
    "fully hybrid": "hybrid",
    "hybrid working": "hybrid",
    "hybrid work": "hybrid",
    "on site": "onsite",
    "on-site": "onsite",
    "in office": "onsite",
    "in-office": "onsite",
    "office based": "onsite",
    "office-based": "onsite",
    "in person": "onsite",
    "not specified": "unspecified",
    "unknown": "unspecified",
    "n/a": "unspecified",
    "none": "unspecified",
    "null": "unspecified",
}

_SPONSORSHIP_ALIASES: Final[dict[str, str]] = {
    "yes": "explicit_yes",
    "explicit yes": "explicit_yes",
    "available": "explicit_yes",
    "no": "explicit_no",
    "explicit no": "explicit_no",
    "not available": "explicit_no",
    "not specified": "unspecified",
    "unknown": "unspecified",
    "n/a": "unspecified",
    "none": "unspecified",
    "null": "unspecified",
}


def _normalise_enum_input(value: Any, aliases: dict[str, str]) -> Any:
    """Map common synonyms to canonical enum string values."""
    if not isinstance(value, str):
        return value
    cleaned = value.strip().lower()
    return aliases.get(cleaned, cleaned)


class SponsorshipSignal(str, Enum):
    """Visa-sponsorship intent extracted from the description text."""

    EXPLICIT_YES = "explicit_yes"
    EXPLICIT_NO = "explicit_no"
    UNSPECIFIED = "unspecified"


class RemoteFriendly(str, Enum):
    """Work-mode disposition extracted from the description text."""

    REMOTE = "remote"
    HYBRID = "hybrid"
    ONSITE = "onsite"
    UNSPECIFIED = "unspecified"


class ExtractionResult(BaseModel):
    """Structured signals extracted from a single job description."""

    model_config = ConfigDict(extra="ignore")

    required_skills: list[str] = Field(default_factory=list, max_length=20)
    years_experience: int | None = Field(default=None, ge=0, le=40)
    sponsorship_signal: SponsorshipSignal = SponsorshipSignal.UNSPECIFIED
    local_experience_required: bool = False
    remote_friendly: RemoteFriendly = RemoteFriendly.UNSPECIFIED

    @field_validator("required_skills")
    @classmethod
    def _normalise_skills(cls, value: list[str]) -> list[str]:
        """Trim, drop empties, and deduplicate skills case-insensitively."""
        seen: dict[str, str] = {}
        for skill in value:
            cleaned = skill.strip()
            key = cleaned.lower()
            if cleaned and key not in seen:
                seen[key] = cleaned
        return list(seen.values())

    @field_validator("remote_friendly", mode="before")
    @classmethod
    def _normalise_remote_friendly(cls, value: Any) -> Any:
        return _normalise_enum_input(value, _REMOTE_ALIASES)

    @field_validator("sponsorship_signal", mode="before")
    @classmethod
    def _normalise_sponsorship_signal(cls, value: Any) -> Any:
        return _normalise_enum_input(value, _SPONSORSHIP_ALIASES)
