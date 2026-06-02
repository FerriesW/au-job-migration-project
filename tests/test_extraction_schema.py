"""Unit tests for the LLM extraction output contract (pure logic, no cloud calls).

These cover the normalisation boundary that bit the project in v0.4.0: Qwen
emits paraphrases like "fully remote" or "yes", which must coerce to the
canonical enum values rather than fail validation.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from adzuna_pipeline.extraction.schema import (
    ExtractionResult,
    RemoteFriendly,
    SponsorshipSignal,
)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("fully remote", RemoteFriendly.REMOTE),
        ("WFH", RemoteFriendly.REMOTE),
        ("on-site", RemoteFriendly.ONSITE),
        ("hybrid working", RemoteFriendly.HYBRID),
        ("not specified", RemoteFriendly.UNSPECIFIED),
        ("remote", RemoteFriendly.REMOTE),
    ],
)
def test_remote_friendly_synonyms_normalise(raw: str, expected: RemoteFriendly) -> None:
    assert ExtractionResult(remote_friendly=raw).remote_friendly is expected


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("yes", SponsorshipSignal.EXPLICIT_YES),
        ("available", SponsorshipSignal.EXPLICIT_YES),
        ("no", SponsorshipSignal.EXPLICIT_NO),
        ("n/a", SponsorshipSignal.UNSPECIFIED),
    ],
)
def test_sponsorship_synonyms_normalise(raw: str, expected: SponsorshipSignal) -> None:
    assert ExtractionResult(sponsorship_signal=raw).sponsorship_signal is expected


def test_required_skills_dedupe_is_case_insensitive_and_trims() -> None:
    result = ExtractionResult(required_skills=["AWS", "aws", " Python ", "python", ""])
    assert result.required_skills == ["AWS", "Python"]


def test_defaults_are_conservative() -> None:
    result = ExtractionResult()
    assert result.required_skills == []
    assert result.years_experience is None
    assert result.local_experience_required is False
    assert result.sponsorship_signal is SponsorshipSignal.UNSPECIFIED
    assert result.remote_friendly is RemoteFriendly.UNSPECIFIED


def test_years_experience_out_of_range_is_rejected() -> None:
    with pytest.raises(ValidationError):
        ExtractionResult(years_experience=99)
