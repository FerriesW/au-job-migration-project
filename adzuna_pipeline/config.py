"""Typed runtime configuration backed by environment variables."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

PROJECT_ROOT: Path = Path(__file__).resolve().parent.parent
_ENV_FILE: Path = PROJECT_ROOT / ".env"


class _BaseSettings(BaseSettings):
    """Common settings configuration loading values from the project ``.env``."""

    model_config = SettingsConfigDict(env_file=_ENV_FILE, extra="ignore")


class AdzunaSettings(_BaseSettings):
    """Adzuna API credentials and target country."""

    app_id: str = Field(..., alias="ADZUNA_APP_ID")
    app_key: str = Field(..., alias="ADZUNA_APP_KEY")
    country: str = Field("au", alias="ADZUNA_COUNTRY")


class GcpSettings(_BaseSettings):
    """Google Cloud project, dataset, and storage bucket configuration."""

    credentials_path: str = Field(..., alias="GOOGLE_APPLICATION_CREDENTIALS")
    project_id: str = Field(..., alias="GCP_PROJECT_ID")
    location: str = Field("australia-southeast1", alias="GCP_LOCATION")
    bucket_raw: str = Field(..., alias="GCS_BUCKET_RAW")
    dataset_raw: str = Field("raw", alias="BQ_DATASET_RAW")
    dataset_staging: str = Field("staging", alias="BQ_DATASET_STAGING")
    dataset_marts: str = Field("marts", alias="BQ_DATASET_MARTS")


class DashScopeSettings(_BaseSettings):
    """Alibaba DashScope (Qwen) API configuration."""

    api_key: str = Field(..., alias="DASHSCOPE_API_KEY")
    model: str = Field("qwen-turbo", alias="QWEN_MODEL")


class RuntimeSettings(_BaseSettings):
    """Runtime knobs shared across pipeline components."""

    log_level: str = Field("INFO", alias="LOG_LEVEL")
    sample_limit: int = Field(0, alias="SAMPLE_LIMIT")


@lru_cache(maxsize=1)
def get_adzuna() -> AdzunaSettings:
    """Return cached Adzuna settings."""
    return AdzunaSettings()


@lru_cache(maxsize=1)
def get_gcp() -> GcpSettings:
    """Return cached GCP settings."""
    return GcpSettings()


@lru_cache(maxsize=1)
def get_dashscope() -> DashScopeSettings:
    """Return cached DashScope settings."""
    return DashScopeSettings()


@lru_cache(maxsize=1)
def get_runtime() -> RuntimeSettings:
    """Return cached runtime settings."""
    return RuntimeSettings()
