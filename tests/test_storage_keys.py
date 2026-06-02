"""Unit tests for the GCS object-key builder (pure logic, no cloud calls)."""

from __future__ import annotations

from datetime import date

from adzuna_pipeline.storage import build_blob_key


def test_build_blob_key_canonical_layout() -> None:
    key = build_blob_key(snapshot_date=date(2026, 5, 6), partition_label="Melbourne")
    assert key == "adzuna/snapshot_date=2026-05-06/melbourne.jsonl.gz"


def test_build_blob_key_lowercases_and_hyphenates_label() -> None:
    key = build_blob_key(snapshot_date=date(2026, 5, 6), partition_label="Brisbane Region")
    assert key == "adzuna/snapshot_date=2026-05-06/brisbane-region.jsonl.gz"


def test_build_blob_key_honours_custom_extension() -> None:
    key = build_blob_key(snapshot_date=date(2026, 1, 1), partition_label="Sydney", extension="json")
    assert key == "adzuna/snapshot_date=2026-01-01/sydney.json"
