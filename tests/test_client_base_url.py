"""Unit tests for DashScope base-URL resolution and validation (pure, no cloud).

Guards the v0.4.0 incident where a duplicated ``.env`` line
(``DASHSCOPE_BASE_URL=DASHSCOPE_BASE_URL=https://...``) produced an opaque
transport error instead of a clear configuration message.
"""

from __future__ import annotations

import pytest

from adzuna_pipeline.extraction.client import (
    DASHSCOPE_BASE_URL_DEFAULT,
    ExtractionError,
    _resolve_base_url,
    _validate_base_url,
)


def test_validate_base_url_accepts_https() -> None:
    assert _validate_base_url("https://example.com/v1") == "https://example.com/v1"


def test_validate_base_url_rejects_missing_scheme() -> None:
    with pytest.raises(ExtractionError):
        _validate_base_url("DASHSCOPE_BASE_URL=https://oops")


def test_resolve_base_url_defaults_when_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DASHSCOPE_BASE_URL", raising=False)
    assert _resolve_base_url() == DASHSCOPE_BASE_URL_DEFAULT


def test_resolve_base_url_strips_quotes_and_whitespace(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DASHSCOPE_BASE_URL", '  "https://intl.example/v1"  ')
    assert _resolve_base_url() == "https://intl.example/v1"
