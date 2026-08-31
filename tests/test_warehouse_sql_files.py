"""Structural checks on the versioned warehouse SQL (pure logic, no cloud calls).

`aws/glue/` and `snowflake/` hold the catalogue and landing-layer definitions.
Neither is exercised by the hermetic CI gate — applying them needs credentials —
so these tests guard the properties that can be checked from the text alone,
and that would otherwise only surface as a failed run against a live warehouse.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
GLUE_DIR = REPO / "aws" / "glue"
SNOWFLAKE_DIR = REPO / "snowflake"


def _body(path: Path) -> str:
    """Return the file's SQL with `--` comment lines removed."""
    lines = path.read_text(encoding="utf-8").splitlines()
    return "\n".join(line for line in lines if not line.lstrip().startswith("--")).strip()


def _glue_files() -> list[Path]:
    return sorted(GLUE_DIR.glob("*.sql"))


def _snowflake_files() -> list[Path]:
    return sorted(SNOWFLAKE_DIR.glob("*.sql"))


def test_both_directories_hold_sql() -> None:
    """Guard the guard: an empty glob would make every parametrised test vacuous."""
    assert _glue_files(), f"no .sql under {GLUE_DIR}"
    assert _snowflake_files(), f"no .sql under {SNOWFLAKE_DIR}"


@pytest.mark.parametrize("path", _glue_files(), ids=lambda p: p.name)
def test_glue_file_holds_exactly_one_statement(path: Path) -> None:
    """Athena executes one statement per call, so a stray `;` would truncate a file.

    The applier strips a single trailing semicolon; anything beyond that means
    a second statement that would be silently dropped.
    """
    assert ";" not in _body(path).rstrip(";"), (
        f"{path.name} appears to contain more than one statement"
    )


@pytest.mark.parametrize("path", _glue_files() + _snowflake_files(), ids=lambda p: p.name)
def test_file_has_a_body(path: Path) -> None:
    assert _body(path), f"{path.name} is comments only"


@pytest.mark.parametrize("path", _snowflake_files(), ids=lambda p: p.name)
def test_snowflake_file_is_idempotent(path: Path) -> None:
    """Every Snowflake object must be creatable twice without error.

    These files are pasted into a Snowsight worksheet by hand and re-run when
    something further down fails. A bare CREATE would abort the rest of the
    worksheet on the second pass, leaving the account half-built.
    """
    body = _body(path).upper()
    creates = [line for line in body.splitlines() if line.strip().startswith("CREATE ")]
    offenders = [
        line.strip() for line in creates if "IF NOT EXISTS" not in line and "OR REPLACE" not in line
    ]
    assert not offenders, f"{path.name} has non-idempotent CREATE: {offenders}"


def test_glue_ddl_declares_projection_not_a_crawler() -> None:
    """Partitions must come from projection.

    Without these properties a table silently returns nothing until someone
    runs MSCK REPAIR — the crawler-shaped maintenance ADR-0002 set out to
    avoid.
    """
    for name in ("02_adzuna_jobs.sql", "03_adzuna_llm_extract.sql"):
        body = _body(GLUE_DIR / name)
        assert "'projection.enabled'" in body, name
        assert "storage.location.template" in body, name


def test_glue_timestamps_are_strings() -> None:
    """ISO 8601 columns must not be declared as Hive `timestamp`.

    Hive's `timestamp` expects `yyyy-MM-dd HH:mm:ss` and yields NULL rather
    than an error on ISO 8601, so the wrong declaration loses data quietly.
    """
    for name in ("02_adzuna_jobs.sql", "03_adzuna_llm_extract.sql"):
        assert "timestamp" not in _body(GLUE_DIR / name).lower(), name


def test_snowflake_secrets_are_placeholders() -> None:
    """No account identifier, ARN or password may be committed.

    `02_storage_integration.sql` needs an AWS role ARN, which embeds the AWS
    account id; it stays a placeholder and is filled in at apply time.
    """
    assignment = re.compile(r"PASSWORD\s*=\s*'([^']*)'", re.IGNORECASE)
    for path in _snowflake_files():
        body = _body(path)
        assert "arn:aws:iam::" not in body, f"{path.name} contains a literal AWS ARN"
        literals = [v for v in assignment.findall(body) if not v.startswith("<")]
        assert not literals, f"{path.name} assigns a literal password"
