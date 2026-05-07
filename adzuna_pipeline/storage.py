"""Cloud Storage utilities for landing raw Adzuna payloads as JSONL."""

from __future__ import annotations

import gzip
import json
import logging
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date, datetime, timezone
from typing import Final

from google.cloud import storage

from .config import get_gcp

LOGGER: Final[logging.Logger] = logging.getLogger(__name__)

DATASET_PREFIX: Final[str] = "adzuna"
PARTITION_KEY: Final[str] = "snapshot_date"


@dataclass(frozen=True)
class UploadResult:
    """Outcome of a single GCS upload.

    Attributes:
        gcs_uri: Fully qualified destination URI in the form ``gs://bucket/key``.
        row_count: Number of records written to the object.
        size_bytes: Size of the gzipped payload in bytes.
        snapshot_date: Logical partition date.
        partition_label: Free-form label describing the upload subset (typically the city).
    """

    gcs_uri: str
    row_count: int
    size_bytes: int
    snapshot_date: date
    partition_label: str


def build_blob_key(
    *,
    snapshot_date: date,
    partition_label: str,
    extension: str = "jsonl.gz",
) -> str:
    """Construct the canonical GCS object key for an Adzuna snapshot."""
    safe_label = partition_label.lower().replace(" ", "-")
    return (
        f"{DATASET_PREFIX}/"
        f"{PARTITION_KEY}={snapshot_date.isoformat()}/"
        f"{safe_label}.{extension}"
    )


class GcsRawUploader:
    """Upload Adzuna payloads to the raw landing bucket as gzipped JSONL.

    Each upload represents one (snapshot_date, partition_label) tuple; the
    object overwrites any prior payload for that key, which makes re-runs of
    the same snapshot date idempotent.
    """

    def __init__(
        self,
        *,
        bucket_name: str | None = None,
        project_id: str | None = None,
    ) -> None:
        settings = get_gcp()
        self._bucket_name: str = bucket_name or settings.bucket_raw
        self._project_id: str = project_id or settings.project_id
        self._client: storage.Client = storage.Client(project=self._project_id)
        self._bucket: storage.Bucket = self._client.bucket(self._bucket_name)

    @property
    def bucket_name(self) -> str:
        """Return the configured bucket name."""
        return self._bucket_name

    def upload_jsonl(
        self,
        rows: Iterable[dict],
        *,
        snapshot_date: date,
        partition_label: str,
        decorate_with_metadata: bool = True,
    ) -> UploadResult:
        """Serialise ``rows`` as gzipped JSONL and upload them.

        Args:
            rows: Iterable of records to write. Each record is JSON-serialised.
            snapshot_date: Logical partition date for this batch.
            partition_label: Subset identifier (typically the city name).
            decorate_with_metadata: When True, augment each record with
                ``snapshot_date``, ``ingested_at``, and ``source_city``
                fields so they survive into BigQuery without joins.

        Returns:
            An ``UploadResult`` describing the destination URI and payload size.
        """
        blob_key = build_blob_key(
            snapshot_date=snapshot_date,
            partition_label=partition_label,
        )
        blob = self._bucket.blob(blob_key)

        ingested_at = datetime.now(tz=timezone.utc).isoformat()
        snapshot_iso = snapshot_date.isoformat()

        line_count = 0
        buffer = bytearray()
        with gzip.GzipFile(fileobj=_BytearrayWriter(buffer), mode="wb") as gzfile:
            for record in rows:
                if decorate_with_metadata:
                    enriched = {
                        **record,
                        "snapshot_date": snapshot_iso,
                        "ingested_at": ingested_at,
                        "source_city": partition_label,
                    }
                else:
                    enriched = record
                gzfile.write(json.dumps(enriched, ensure_ascii=False).encode("utf-8"))
                gzfile.write(b"\n")
                line_count += 1

        blob.cache_control = "no-cache"
        blob.content_encoding = "gzip"
        blob.upload_from_string(bytes(buffer), content_type="application/x-ndjson")

        gcs_uri = f"gs://{self._bucket_name}/{blob_key}"
        LOGGER.info(
            "Uploaded %s rows (%s bytes) to %s",
            line_count, len(buffer), gcs_uri,
        )
        return UploadResult(
            gcs_uri=gcs_uri,
            row_count=line_count,
            size_bytes=len(buffer),
            snapshot_date=snapshot_date,
            partition_label=partition_label,
        )


class _BytearrayWriter:
    """Minimal file-like adapter so gzip can write into a bytearray buffer."""

    def __init__(self, buffer: bytearray) -> None:
        self._buffer = buffer

    def write(self, data: bytes) -> int:
        self._buffer.extend(data)
        return len(data)

    def flush(self) -> None:  # pragma: no cover - no-op required by gzip
        return None
