"""Structural checks on the versioned Glue DDL (pure logic, no cloud calls)."""

from __future__ import annotations

from pathlib import Path

import pytest

DDL_DIR = Path(__file__).resolve().parent.parent / "aws" / "glue"


def _statement_body(path: Path) -> str:
    """Return the file's SQL with `--` comment lines removed."""
    lines = path.read_text(encoding="utf-8").splitlines()
    return "\n".join(line for line in lines if not line.lstrip().startswith("--")).strip()


def _ddl_files() -> list[Path]:
    return sorted(DDL_DIR.glob("*.sql"))


def test_ddl_directory_is_not_empty() -> None:
    assert _ddl_files(), f"no .sql files found under {DDL_DIR}"


@pytest.mark.parametrize("path", _ddl_files(), ids=lambda p: p.name)
def test_each_file_holds_exactly_one_statement(path: Path) -> None:
    """Athena executes one statement per call, so a stray `;` would truncate a file.

    The applier strips a single trailing semicolon; anything beyond that means
    a second statement that would be silently dropped.
    """
    body = _statement_body(path).rstrip(";")
    assert ";" not in body, f"{path.name} appears to contain more than one statement"


@pytest.mark.parametrize("path", _ddl_files(), ids=lambda p: p.name)
def test_each_file_has_a_body(path: Path) -> None:
    assert _statement_body(path), f"{path.name} is comments only"


def test_table_ddl_declares_projection_not_a_crawler() -> None:
    """Partitions must come from projection.

    Without these properties the table silently returns nothing until someone
    runs MSCK REPAIR — which is exactly the crawler-shaped maintenance ADR-0002
    set out to avoid.
    """
    body = _statement_body(DDL_DIR / "02_adzuna_jobs.sql")
    assert "'projection.enabled'" in body
    assert "storage.location.template" in body


def test_timestamps_are_strings_not_hive_timestamps() -> None:
    """`created` and `ingested_at` arrive as ISO 8601.

    Hive's `timestamp` expects `yyyy-MM-dd HH:mm:ss` and yields NULL rather
    than an error on ISO 8601, so declaring them as `timestamp` would lose the
    data quietly. They are converted at query time instead.
    """
    body = _statement_body(DDL_DIR / "02_adzuna_jobs.sql")
    assert "created             string" in body
    assert "ingested_at         string" in body
    assert "timestamp" not in body.lower()
