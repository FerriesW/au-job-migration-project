"""Unit tests for object-key construction and payload building.

Pure logic only — no cloud calls, so these run in the hermetic CI gate.
"""

from __future__ import annotations

import gzip
import json
from datetime import date

from adzuna_pipeline.storage import EXTRACT_PREFIX, build_blob_key, build_payload


def test_build_blob_key_canonical_layout() -> None:
    key = build_blob_key(snapshot_date=date(2026, 5, 6), partition_label="Melbourne")
    assert key == "adzuna/snapshot_date=2026-05-06/melbourne.jsonl.gz"


def test_build_blob_key_lowercases_and_hyphenates_label() -> None:
    key = build_blob_key(snapshot_date=date(2026, 5, 6), partition_label="Brisbane Region")
    assert key == "adzuna/snapshot_date=2026-05-06/brisbane-region.jsonl.gz"


def test_build_blob_key_honours_custom_extension() -> None:
    key = build_blob_key(snapshot_date=date(2026, 1, 1), partition_label="Sydney", extension="json")
    assert key == "adzuna/snapshot_date=2026-01-01/sydney.json"


def test_payload_derives_the_canonical_key() -> None:
    """The key both landing zones write to comes from the payload itself.

    This is what makes the GCS and S3 mirrors structurally identical rather
    than identical by convention: neither uploader chooses its own key.
    """
    payload = build_payload(
        [{"id": "1"}],
        snapshot_date=date(2026, 5, 6),
        partition_label="Melbourne",
    )
    assert payload.blob_key == "adzuna/snapshot_date=2026-05-06/melbourne.jsonl.gz"


def test_payload_is_gzipped_jsonl_with_metadata() -> None:
    payload = build_payload(
        [{"id": "1"}, {"id": "2"}],
        snapshot_date=date(2026, 5, 6),
        partition_label="Melbourne",
    )
    lines = gzip.decompress(payload.data).decode("utf-8").splitlines()

    assert payload.row_count == 2
    assert len(lines) == 2

    first = json.loads(lines[0])
    assert first["id"] == "1"
    assert first["snapshot_date"] == "2026-05-06"
    assert first["source_city"] == "Melbourne"
    assert "ingested_at" in first


def test_payload_metadata_decoration_can_be_disabled() -> None:
    payload = build_payload(
        [{"id": "1"}],
        snapshot_date=date(2026, 5, 6),
        partition_label="Melbourne",
        decorate_with_metadata=False,
    )
    record = json.loads(gzip.decompress(payload.data).decode("utf-8").splitlines()[0])
    assert record == {"id": "1"}


def test_payload_stamps_one_ingested_at_across_all_records() -> None:
    """Every record in a payload shares one timestamp.

    Both landing zones receive these same bytes, so GCS and S3 stay
    byte-identical. Serialising once per cloud would stamp a different
    ingested_at into each copy.
    """
    payload = build_payload(
        [{"id": str(i)} for i in range(50)],
        snapshot_date=date(2026, 5, 6),
        partition_label="Melbourne",
    )
    lines = gzip.decompress(payload.data).decode("utf-8").splitlines()
    stamps = {json.loads(line)["ingested_at"] for line in lines}
    assert len(stamps) == 1


def test_extract_prefix_produces_a_sibling_dataset_key() -> None:
    """LLM extracts live beside the raw snapshots, not inside them.

    `adzuna_llm_extract/` must not sit under `adzuna/`, or the Glue table and
    the Snowflake stage over the raw prefix would swallow records with an
    entirely different shape.
    """
    key = build_blob_key(
        snapshot_date=date(2026, 5, 7),
        partition_label="extract",
        dataset_prefix=EXTRACT_PREFIX,
    )
    assert key == "adzuna_llm_extract/snapshot_date=2026-05-07/extract.jsonl.gz"
    assert not key.startswith("adzuna/")


def test_payload_bytes_are_reproducible() -> None:
    """The same records must compress to the same bytes, run after run.

    gzip writes a modification timestamp into its header. Left at the default
    this made identical content checksum differently every time, so the
    GCS/S3 reconciler would rewrite unchanged objects and "in sync" would mean
    nothing. `build_payload` pins mtime=0; this is the guard on that.
    """
    records = [{"job_id": "1", "skill": "dbt"}, {"job_id": "2", "skill": "sql"}]
    kwargs = {
        "snapshot_date": date(2026, 5, 7),
        "partition_label": "extract",
        "dataset_prefix": EXTRACT_PREFIX,
        "decorate_with_metadata": False,
    }
    first = build_payload(records, **kwargs)  # type: ignore[arg-type]
    second = build_payload(records, **kwargs)  # type: ignore[arg-type]
    assert first.data == second.data


def test_payload_size_matches_serialised_bytes() -> None:
    payload = build_payload(
        [{"id": "1"}],
        snapshot_date=date(2026, 5, 6),
        partition_label="Melbourne",
    )
    assert payload.size_bytes == len(payload.data)
