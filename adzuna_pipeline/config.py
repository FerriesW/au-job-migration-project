"""Type-safe configuration loaded from environment / .env file.

Single source of truth for credentials and pipeline knobs. Fail loud at import
time if a required value is missing — never let downstream code silently use a
default-empty key.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

PROJECT_ROOT = Path(__file__).resolve().parent.parent


class AdzunaSettings(BaseSettings):
    app_id: str = Field(..., alias="ADZUNA_APP_ID")
    app_key: str = Field(..., alias="ADZUNA_APP_KEY")
    country: str = Field("au", alias="ADZUNA_COUNTRY")

    model_config = SettingsConfigDict(env_file=PROJECT_ROOT / ".env", extra="ignore")


class GcpSettings(BaseSettings):
    credentials_path: str = Field(..., alias="GOOGLE_APPLICATION_CREDENTIALS")
    project_id: str = Field(..., alias="GCP_PROJECT_ID")
    location: str = Field("australia-southeast1", alias="GCP_LOCATION")
    bucket_raw: str = Field(..., alias="GCS_BUCKET_RAW")
    dataset_raw: str = Field("raw", alias="BQ_DATASET_RAW")
    dataset_staging: str = Field("staging", alias="BQ_DATASET_STAGING")
    dataset_marts: str = Field("marts", alias="BQ_DATASET_MARTS")

    model_config = SettingsConfigDict(env_file=PROJECT_ROOT / ".env", extra="ignore")


class DashScopeSettings(BaseSettings):
    api_key: str = Field(..., alias="DASHSCOPE_API_KEY")
    model: str = Field("qwen-turbo", alias="QWEN_MODEL")

    model_config = SettingsConfigDict(env_file=PROJECT_ROOT / ".env", extra="ignore")


class RuntimeSettings(BaseSettings):
    log_level: str = Field("INFO", alias="LOG_LEVEL")
    sample_limit: int = Field(0, alias="SAMPLE_LIMIT")  # 0 = unlimited

    model_config = SettingsConfigDict(env_file=PROJECT_ROOT / ".env", extra="ignore")


@lru_cache(maxsize=1)
def get_adzuna() -> AdzunaSettings:
    return AdzunaSettings()


@lru_cache(maxsize=1)
def get_gcp() -> GcpSettings:
    return GcpSettings()


@lru_cache(maxsize=1)
def get_dashscope() -> DashScopeSettings:
    return DashScopeSettings()


@lru_cache(maxsize=1)
def get_runtime() -> RuntimeSettings:
    return RuntimeSettings()
