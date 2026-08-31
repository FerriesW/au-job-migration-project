"""Unit tests for DashScope base-URL resolution and validation (pure, no cloud).

Guards the v0.4.0 incident where a duplicated ``.env`` line
(``DASHSCOPE_BASE_URL=DASHSCOPE_BASE_URL=https://...``) produced an opaque
transport error instead of a clear configuration message.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from pydantic_settings import SettingsConfigDict

from adzuna_pipeline import config
from adzuna_pipeline.extraction.client import (
    DASHSCOPE_BASE_URL_DEFAULT,
    ExtractionError,
    _resolve_base_url,
    _validate_base_url,
)


@pytest.fixture(autouse=True)
def _clear_settings_cache() -> Iterator[None]:
    """Drop the cached settings around each test.

    `get_dashscope` is `lru_cache`d, so a value read in one test would leak
    into the next. Resolution now goes through the settings class rather than
    `os.getenv`, which is the point — the environment is read in exactly one
    place — but it means the cache has to be cleared to vary it.
    """
    config.get_dashscope.cache_clear()
    yield
    config.get_dashscope.cache_clear()


def test_validate_base_url_accepts_https() -> None:
    assert _validate_base_url("https://example.com/v1") == "https://example.com/v1"


def test_validate_base_url_rejects_missing_scheme() -> None:
    with pytest.raises(ExtractionError):
        _validate_base_url("DASHSCOPE_BASE_URL=https://oops")


def test_resolve_base_url_defaults_when_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(config.DashScopeSettings, "model_config", _no_env_file())
    monkeypatch.delenv("DASHSCOPE_BASE_URL", raising=False)
    monkeypatch.setenv("DASHSCOPE_API_KEY", "test-key")
    assert _resolve_base_url() == DASHSCOPE_BASE_URL_DEFAULT


def test_resolve_base_url_strips_quotes_and_whitespace(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(config.DashScopeSettings, "model_config", _no_env_file())
    monkeypatch.setenv("DASHSCOPE_API_KEY", "test-key")
    monkeypatch.setenv("DASHSCOPE_BASE_URL", '  "https://intl.example/v1"  ')
    assert _resolve_base_url() == "https://intl.example/v1"


def _no_env_file() -> SettingsConfigDict:
    """Settings config that ignores the developer's real `.env`.

    Without this the test would pass or fail depending on what the machine
    running it happens to have configured.
    """
    return SettingsConfigDict(env_file=None, extra="ignore")
