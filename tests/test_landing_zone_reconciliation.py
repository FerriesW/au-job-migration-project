"""Unit tests for the GCS -> S3 reconciliation decision (pure logic, no cloud calls)."""

from __future__ import annotations

from scripts.sync_gcs_to_s3 import Action, Comparison

_MD5_A = "4181e63050566ceb0bffbafe2a4fe541"
_MD5_B = "84d918b8d01d40c47ad94f9d7546f61f"


def _comparison(*, s3_md5: str | None) -> Comparison:
    return Comparison(
        key="adzuna/snapshot_date=2026-05-07/melbourne.jsonl.gz",
        size_bytes=340118,
        gcs_md5=_MD5_A,
        s3_md5=s3_md5,
    )


def test_matching_checksums_need_no_copy() -> None:
    comparison = _comparison(s3_md5=_MD5_A)
    assert comparison.action is Action.IN_SYNC
    assert not comparison.needs_copy


def test_absent_from_s3_is_a_copy() -> None:
    comparison = _comparison(s3_md5=None)
    assert comparison.action is Action.MISSING
    assert comparison.needs_copy


def test_differing_checksums_are_a_copy_not_a_skip() -> None:
    """Presence alone must not satisfy reconciliation.

    A partition written to both clouds in separate runs carries a different
    ingested_at in each copy, so the object exists on both sides while the
    bytes differ. Comparing keys rather than digests would call that in sync.
    """
    comparison = _comparison(s3_md5=_MD5_B)
    assert comparison.action is Action.DIVERGED
    assert comparison.needs_copy
