"""Sync BigQuery raw-layer tables into Snowflake for the cross-warehouse build.

Lifts the two pipeline-loaded tables that dbt does not own — ``raw.adzuna_jobs``
and ``staging.adzuna_jobs_llm_extract`` — from BigQuery into the Snowflake
``AU_JOBS_RADAR`` database, so ``dbt build --target dev_sf`` can materialise the
staging / intermediate / marts layers from the same model files used on
BigQuery. The four CSV seeds are re-created by ``dbt seed --target dev_sf`` and
are NOT synced here.

Mechanism (COPY INTO):

    BigQuery -> Arrow -> local Parquet -> Snowflake table stage (PUT)
    -> COPY INTO ... MATCH_BY_COLUMN_NAME = CASE_INSENSITIVE

Nested BigQuery RECORD / REPEATED columns (``location`` / ``company`` /
``category`` / ``required_skills``) land in Snowflake VARIANT / ARRAY columns;
the warehouse-specific access is handled in the dbt staging models.

Usage:
    uv run --env-file .env python scripts/sync_bq_to_snowflake.py
    uv run --env-file .env python scripts/sync_bq_to_snowflake.py --dry-run
"""

from __future__ import annotations

import logging
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

import pyarrow.parquet as pq
import snowflake.connector
import typer
from dotenv import load_dotenv
from google.cloud import bigquery
from rich.console import Console
from rich.table import Table

PROJECT_ROOT: Final[Path] = Path(__file__).resolve().parent.parent
load_dotenv(PROJECT_ROOT / ".env")

from adzuna_pipeline.config import get_gcp, get_snowflake  # noqa: E402

LOGGER: Final[logging.Logger] = logging.getLogger(__name__)
console = Console()
app = typer.Typer(add_completion=False, help="Sync BigQuery raw tables into Snowflake.")


# Snowflake DDL. Scalars are typed; nested BigQuery RECORD/REPEATED columns
# become VARIANT/ARRAY. {db} is interpolated with the configured database.
_ADZUNA_JOBS_DDL: Final[str] = """
create or replace table {db}.RAW.ADZUNA_JOBS (
    snapshot_date        date,
    ingested_at          timestamp_ntz,
    source_city          string,
    id                   string,
    title                string,
    description          string,
    created              timestamp_ntz,
    redirect_url         string,
    adref                string,
    salary_min           float,
    salary_max           float,
    salary_is_predicted  string,
    contract_type        string,
    contract_time        string,
    latitude             float,
    longitude            float,
    location             variant,
    company              variant,
    category             variant
)
"""

_LLM_EXTRACT_DDL: Final[str] = """
create or replace table {db}.STAGING.ADZUNA_JOBS_LLM_EXTRACT (
    job_id                      string,
    snapshot_date               date,
    required_skills             array,
    years_experience            number,
    sponsorship_signal          string,
    local_experience_required   boolean,
    remote_friendly             string,
    model_version               string,
    extracted_at                timestamp_ntz,
    extraction_status           string,
    error_message               string
)
"""


@dataclass(frozen=True)
class TableSpec:
    """One BigQuery -> Snowflake table mapping."""

    bq_dataset: str
    bq_table: str
    sf_schema: str
    sf_table: str
    create_ddl: str


@dataclass(frozen=True)
class SyncResult:
    """Per-table outcome of the sync."""

    spec: TableSpec
    bq_rows: int
    sf_rows: int

    @property
    def ok(self) -> bool:
        return self.bq_rows == self.sf_rows


def _extract_to_parquet(
    bq: bigquery.Client, project: str, dataset: str, table: str, dest: Path
) -> int:
    """Query a BigQuery table to Arrow and write it to a local Parquet file."""
    fqn = f"`{project}.{dataset}.{table}`"
    arrow_table = bq.query(f"select * from {fqn}").result().to_arrow(create_bqstorage_client=False)
    pq.write_table(arrow_table, dest)  # type: ignore[no-untyped-call]
    return int(arrow_table.num_rows)


def _load_into_snowflake(cursor: Any, database: str, spec: TableSpec, parquet_path: Path) -> int:
    """Create the target table, PUT the Parquet to its stage, and COPY INTO it."""
    cursor.execute(spec.create_ddl.format(db=database))
    cursor.execute(f"use schema {database}.{spec.sf_schema}")
    # Table stage (@%TABLE) is scoped to the current schema set above.
    cursor.execute(
        f"put 'file://{parquet_path.as_posix()}' @%{spec.sf_table} "
        f"overwrite = true auto_compress = false"
    )
    cursor.execute(
        f"copy into {spec.sf_table} from @%{spec.sf_table} "
        f"file_format = (type = parquet) "
        f"match_by_column_name = case_insensitive "
        f"purge = true"
    )
    cursor.execute(f"select count(*) from {spec.sf_table}")
    row = cursor.fetchone()
    return int(row[0]) if row else 0


@app.command()
def main(
    dry_run: bool = typer.Option(
        False,
        "--dry-run",
        help="Extract to Parquet and report counts, but skip the Snowflake load.",
    ),
) -> None:
    """Sync the BigQuery raw-layer tables into Snowflake via COPY INTO."""
    logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)-7s | %(message)s")
    for noisy in ("google.auth", "urllib3", "snowflake.connector"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    gcp = get_gcp()
    bq = bigquery.Client(project=gcp.project_id, location=gcp.location)
    specs = [
        TableSpec(gcp.dataset_raw, "adzuna_jobs", "RAW", "ADZUNA_JOBS", _ADZUNA_JOBS_DDL),
        TableSpec(
            gcp.dataset_staging,
            "adzuna_jobs_llm_extract",
            "STAGING",
            "ADZUNA_JOBS_LLM_EXTRACT",
            _LLM_EXTRACT_DDL,
        ),
    ]

    with tempfile.TemporaryDirectory() as tmp:
        tmpdir = Path(tmp)
        extracted: list[tuple[TableSpec, Path, int]] = []
        for spec in specs:
            dest = tmpdir / f"{spec.sf_table}.parquet"
            rows = _extract_to_parquet(bq, gcp.project_id, spec.bq_dataset, spec.bq_table, dest)
            console.print(
                f"BigQuery [cyan]{spec.bq_dataset}.{spec.bq_table}[/cyan]: "
                f"{rows:,} rows -> {dest.name}"
            )
            extracted.append((spec, dest, rows))

        if dry_run:
            console.print("[yellow]Dry run: Parquet extracted, Snowflake load skipped.[/yellow]")
            raise typer.Exit(0)

        sf = get_snowflake()
        conn = snowflake.connector.connect(
            account=sf.account,
            user=sf.user,
            password=sf.password,
            role=sf.role,
            warehouse=sf.warehouse,
            database=sf.database,
        )
        results: list[SyncResult] = []
        try:
            cursor = conn.cursor()
            for spec, dest, bq_rows in extracted:
                sf_rows = _load_into_snowflake(cursor, sf.database, spec, dest)
                console.print(
                    f"Snowflake [green]{sf.database}.{spec.sf_schema}.{spec.sf_table}[/green]: "
                    f"loaded {sf_rows:,} rows"
                )
                results.append(SyncResult(spec, bq_rows, sf_rows))
        finally:
            conn.close()

    summary = Table(title="BigQuery -> Snowflake sync")
    summary.add_column("Table", style="cyan")
    summary.add_column("BigQuery", justify="right")
    summary.add_column("Snowflake", justify="right")
    summary.add_column("Status", justify="center")
    for r in results:
        status = "[green]OK[/green]" if r.ok else "[red]MISMATCH[/red]"
        summary.add_row(
            f"{r.spec.sf_schema}.{r.spec.sf_table}", f"{r.bq_rows:,}", f"{r.sf_rows:,}", status
        )
    console.print(summary)

    if not all(r.ok for r in results):
        console.print("[red]Row-count mismatch detected.[/red]")
        raise typer.Exit(1)
    console.print("[bold green]Sync complete; row counts match.[/bold green]")


if __name__ == "__main__":
    app()
