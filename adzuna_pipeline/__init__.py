"""Adzuna ingestion pipeline."""

from .client import AdzunaApiError, AdzunaClient, SearchPage, SearchQuery
from .loader import ADZUNA_JOBS_SCHEMA, BigQueryRawLoader, LoadResult
from .storage import (
    GcsRawUploader,
    RawPayload,
    RawUploader,
    S3RawUploader,
    UploadResult,
    build_blob_key,
    build_payload,
)

__version__ = "0.1.0"

__all__ = [
    "ADZUNA_JOBS_SCHEMA",
    "AdzunaApiError",
    "AdzunaClient",
    "BigQueryRawLoader",
    "GcsRawUploader",
    "LoadResult",
    "RawPayload",
    "RawUploader",
    "S3RawUploader",
    "SearchPage",
    "SearchQuery",
    "UploadResult",
    "build_blob_key",
    "build_payload",
]
