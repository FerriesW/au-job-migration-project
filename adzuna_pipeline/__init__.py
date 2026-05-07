"""Adzuna ingestion pipeline."""

from .client import AdzunaApiError, AdzunaClient, SearchPage, SearchQuery
from .loader import ADZUNA_JOBS_SCHEMA, BigQueryRawLoader, LoadResult
from .storage import GcsRawUploader, UploadResult, build_blob_key

__version__ = "0.1.0"

__all__ = [
    "ADZUNA_JOBS_SCHEMA",
    "AdzunaApiError",
    "AdzunaClient",
    "BigQueryRawLoader",
    "GcsRawUploader",
    "LoadResult",
    "SearchPage",
    "SearchQuery",
    "UploadResult",
    "build_blob_key",
]
