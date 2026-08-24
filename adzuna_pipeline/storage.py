"""Object-storage utilities for landing raw Adzuna payloads as JSONL.

The pipeline writes each snapshot to two landing zones — Google Cloud Storage
and Amazon S3 — that are byte-for-byte mirrors of each other. To make that
guarantee structural rather than incidental, a snapshot is serialised exactly
once into a :class:`RawPayload`, and both uploaders write those same bytes
under the key the payload derives for itself.
"""

from __future__ import annotations

import gzip
import json
import logging
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import Any, Final, Protocol

import boto3
from google.cloud import storage  # type: ignore[attr-defined]

from .config import get_aws, get_gcp

LOGGER: Final[logging.Logger] = logging.getLogger(__name__)

DATASET_PREFIX: Final[str] = "adzuna"
EXTRACT_PREFIX: Final[str] = "adzuna_llm_extract"
PARTITION_KEY: Final[str] = "snapshot_date"


def build_blob_key(
    *,
    snapshot_date: date,
    partition_label: str,
    dataset_prefix: str = DATASET_PREFIX,
    extension: str = "jsonl.gz",
) -> str:
    """Construct the canonical object key for one landing-zone object.

    The ``snapshot_date=`` segment is Hive-style partitioning, which is what
    Glue/Athena partition discovery and Snowflake staged-path pruning both
    expect. The same key is used in every landing zone.

    ``dataset_prefix`` selects the dataset: raw Adzuna snapshots under
    ``adzuna/``, LLM extraction results under ``adzuna_llm_extract/``. Keeping
    them in sibling prefixes lets each be catalogued as its own table while a
    single stage or crawler root still covers both.
    """
    safe_label = partition_label.lower().replace(" ", "-")
    return f"{dataset_prefix}/{PARTITION_KEY}={snapshot_date.isoformat()}/{safe_label}.{extension}"


@dataclass(frozen=True)
class RawPayload:
    """One serialised snapshot, ready to be written to any landing zone.

    Attributes:
        data: Gzipped JSONL bytes.
        row_count: Number of records the payload contains.
        snapshot_date: Logical partition date.
        partition_label: Free-form label describing the subset (typically the city).
        dataset_prefix: Top-level key prefix selecting the dataset.
    """

    data: bytes
    row_count: int
    snapshot_date: date
    partition_label: str
    dataset_prefix: str = DATASET_PREFIX

    @property
    def blob_key(self) -> str:
        """Return the object key this payload belongs under."""
        return build_blob_key(
            snapshot_date=self.snapshot_date,
            partition_label=self.partition_label,
            dataset_prefix=self.dataset_prefix,
        )

    @property
    def size_bytes(self) -> int:
        """Return the size of the gzipped payload."""
        return len(self.data)


@dataclass(frozen=True)
class UploadResult:
    """Outcome of writing a payload to one landing zone.

    Attributes:
        uri: Fully qualified destination URI (``gs://bucket/key`` or ``s3://bucket/key``).
        row_count: Number of records written to the object.
        size_bytes: Size of the gzipped payload in bytes.
        snapshot_date: Logical partition date.
        partition_label: Free-form label describing the upload subset.
    """

    uri: str
    row_count: int
    size_bytes: int
    snapshot_date: date
    partition_label: str


def build_payload(
    rows: Iterable[dict[str, Any]],
    *,
    snapshot_date: date,
    partition_label: str,
    dataset_prefix: str = DATASET_PREFIX,
    decorate_with_metadata: bool = True,
) -> RawPayload:
    """Serialise ``rows`` as gzipped JSONL exactly once.

    Args:
        rows: Iterable of records to write. Each record is JSON-serialised.
        snapshot_date: Logical partition date for this batch.
        partition_label: Subset identifier (typically the city name).
        dataset_prefix: Top-level key prefix selecting the dataset.
        decorate_with_metadata: When True, augment each record with
            ``snapshot_date``, ``ingested_at``, and ``source_city`` fields so
            they survive into the warehouse without joins. Leave it off for
            records that already carry their own keys and whose bytes must stay
            reproducible — a fresh ``ingested_at`` on every run would change the
            checksum even when the content had not.

    Returns:
        A ``RawPayload`` holding the compressed bytes and their row count.
    """
    ingested_at = datetime.now(tz=UTC).isoformat()
    snapshot_iso = snapshot_date.isoformat()

    line_count = 0
    buffer = bytearray()
    # mtime=0 keeps the output a pure function of the input. gzip stores a
    # modification timestamp in its header, so the default would give identical
    # content a different checksum on every run — which would make the
    # GCS/S3 reconciler rewrite unchanged objects and rob "in sync" of meaning.
    with gzip.GzipFile(fileobj=_BytearrayWriter(buffer), mode="wb", mtime=0) as gzfile:
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

    return RawPayload(
        data=bytes(buffer),
        row_count=line_count,
        snapshot_date=snapshot_date,
        partition_label=partition_label,
        dataset_prefix=dataset_prefix,
    )


class RawUploader(Protocol):
    """Contract shared by the per-cloud landing-zone uploaders.

    Implementations differ only in which object store they target; the key and
    the bytes come from the payload, so the two landing zones cannot drift.
    """

    @property
    def bucket_name(self) -> str:
        """Return the configured bucket name."""
        ...

    def upload(self, payload: RawPayload) -> UploadResult:
        """Write ``payload`` to this landing zone, overwriting any prior object."""
        ...


class GcsRawUploader:
    """Write payloads to the Google Cloud Storage raw landing bucket.

    Each upload overwrites any prior object for the same key, which makes
    re-runs of the same snapshot date idempotent.
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

    def upload(self, payload: RawPayload) -> UploadResult:
        """Write ``payload`` to GCS and return the destination URI."""
        blob = self._bucket.blob(payload.blob_key)
        blob.cache_control = "no-cache"
        blob.content_encoding = "gzip"
        blob.upload_from_string(payload.data, content_type="application/x-ndjson")

        uri = f"gs://{self._bucket_name}/{payload.blob_key}"
        LOGGER.info("Uploaded %s rows (%s bytes) to %s", payload.row_count, payload.size_bytes, uri)
        return UploadResult(
            uri=uri,
            row_count=payload.row_count,
            size_bytes=payload.size_bytes,
            snapshot_date=payload.snapshot_date,
            partition_label=payload.partition_label,
        )


class S3RawUploader:
    """Write payloads to the Amazon S3 raw landing bucket.

    Credentials come from boto3's own resolution chain, so the same code runs
    locally against an IAM user's access key and in CI against a keyless OIDC
    role. As with GCS, writing the same key twice overwrites, keeping re-runs
    idempotent.
    """

    def __init__(
        self,
        *,
        bucket_name: str | None = None,
        region: str | None = None,
    ) -> None:
        settings = get_aws()
        self._bucket_name: str = bucket_name or settings.bucket_raw
        self._region: str = region or settings.region
        self._client: Any = boto3.client("s3", region_name=self._region)

    @property
    def bucket_name(self) -> str:
        """Return the configured bucket name."""
        return self._bucket_name

    def upload(self, payload: RawPayload) -> UploadResult:
        """Write ``payload`` to S3 and return the destination URI."""
        self._client.put_object(
            Bucket=self._bucket_name,
            Key=payload.blob_key,
            Body=payload.data,
            ContentType="application/x-ndjson",
            ContentEncoding="gzip",
            CacheControl="no-cache",
        )

        uri = f"s3://{self._bucket_name}/{payload.blob_key}"
        LOGGER.info("Uploaded %s rows (%s bytes) to %s", payload.row_count, payload.size_bytes, uri)
        return UploadResult(
            uri=uri,
            row_count=payload.row_count,
            size_bytes=payload.size_bytes,
            snapshot_date=payload.snapshot_date,
            partition_label=payload.partition_label,
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
