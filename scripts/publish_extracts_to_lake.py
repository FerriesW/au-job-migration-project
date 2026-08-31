"""Publish the LLM extraction table from BigQuery into both landing zones.

The raw Adzuna path writes the object stores first and loads BigQuery from
them. This one runs the other way, and deliberately so: extraction is
incremental — `extract_llm_signals.py` fetches only the jobs that lack a
result and MERGEs them — so the authoritative, complete set lives in the
warehouse. Writing just the latest batch to the lake would overwrite an object
with a fraction of its own content.

So the warehouse table is exported whole, one object per snapshot date. That
keeps the landing zone a mirror of the table rather than a change log, and
keeps the "overwriting a key is idempotent" invariant the rest of the pipeline
relies on.

The consequence is worth stating plainly: for *derived* data the AWS and
Snowflake paths depend on BigQuery. Only the raw path is genuinely independent
across clouds.

Rows are emitted in (snapshot_date, job_id) order and carry no ingest-time
metadata, so re-publishing an unchanged table produces byte-identical objects
and the reconciler reports them in sync rather than rewriting them.
"""

from __future__ import annotations

import logging
import sys
from collections import defaultdict
from datetime import date
from typing import Any, Final

import typer
from dotenv import load_dotenv
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from adzuna_pipeline.cli import PROJECT_ROOT, configure_logging

load_dotenv(PROJECT_ROOT / ".env")

# Module imports must follow load_dotenv so config classes pick up env values.
from google.cloud import bigquery  # noqa: E402

from adzuna_pipeline.config import get_gcp  # noqa: E402
from adzuna_pipeline.storage import (  # noqa: E402
    EXTRACT_PREFIX,
    GcsRawUploader,
    RawPayload,
    S3RawUploader,
    build_payload,
)

LOGGER: Final[logging.Logger] = logging.getLogger(__name__)

EXTRACT_TABLE: Final[str] = "adzuna_jobs_llm_extract"
PARTITION_LABEL: Final[str] = "extract"

app = typer.Typer(add_completion=False, help="Publish LLM extracts to the landing zones.")
console = Console()


def _to_record(row: Any) -> dict[str, Any]:
    """Shape one BigQuery row into the JSON written to the landing zone.

    Field names and types mirror `ExtractionRecord.to_bq_row`, so the object in
    the lake and the row in the warehouse describe the same thing in the same
    words. Dates and timestamps become ISO 8601 strings, which is what the
    Glue table and Snowflake's VARIANT layer both expect to parse.
    """
    return {
        "job_id": row.job_id,
        "snapshot_date": row.snapshot_date.isoformat(),
        "required_skills": list(row.required_skills or []),
        "years_experience": row.years_experience,
        "sponsorship_signal": row.sponsorship_signal,
        "local_experience_required": row.local_experience_required,
        "remote_friendly": row.remote_friendly,
        "model_version": row.model_version,
        "extracted_at": row.extracted_at.isoformat(),
        "extraction_status": row.extraction_status,
        "error_message": row.error_message,
    }


def _fetch_by_snapshot(
    client: bigquery.Client, table_id: str, snapshot: date | None
) -> dict[date, list[dict[str, Any]]]:
    """Read the extract table, grouped by snapshot date and ordered for determinism."""
    where = "where snapshot_date = @snapshot" if snapshot else ""
    params = [bigquery.ScalarQueryParameter("snapshot", "DATE", snapshot)] if snapshot else []
    sql = f"""
        select job_id, snapshot_date, required_skills, years_experience,
               sponsorship_signal, local_experience_required, remote_friendly,
               model_version, extracted_at, extraction_status, error_message
        from `{table_id}`
        {where}
        order by snapshot_date, job_id
    """
    grouped: dict[date, list[dict[str, Any]]] = defaultdict(list)
    job_config = bigquery.QueryJobConfig(query_parameters=params)
    for row in client.query(sql, job_config=job_config).result():
        grouped[row.snapshot_date].append(_to_record(row))
    return dict(grouped)


def _render(payloads: list[RawPayload], results: dict[str, tuple[str, str]]) -> Table:
    table = Table(title="LLM extracts published to the landing zones")
    table.add_column("Object key", overflow="fold")
    table.add_column("Rows", justify="right")
    table.add_column("Size", justify="right")
    table.add_column("Mirrors match")
    for payload in payloads:
        gcs_uri, s3_uri = results.get(payload.blob_key, ("", ""))
        state = "[green]yes[/green]" if gcs_uri and s3_uri else "[red]no[/red]"
        table.add_row(
            payload.blob_key,
            f"{payload.row_count:,}",
            f"{payload.size_bytes:,} B",
            state,
        )
    return table


@app.command()
def main(
    snapshot_date: str = typer.Option(
        "",
        help="Publish only this snapshot date (YYYY-MM-DD); default is every date.",
    ),
    dry_run: bool = typer.Option(
        False,
        "--dry-run",
        help="Report what would be written without uploading anything.",
    ),
    log_level: str = typer.Option("INFO", help="Logging level."),
) -> None:
    """Export the extract table to `adzuna_llm_extract/` in GCS and S3."""
    configure_logging(log_level)
    gcp = get_gcp()
    snapshot = date.fromisoformat(snapshot_date) if snapshot_date else None
    table_id = f"{gcp.project_id}.{gcp.dataset_staging}.{EXTRACT_TABLE}"

    console.print(
        Panel.fit(
            f"[bold]Publish LLM extracts[/bold]\n"
            f"source=[cyan]{table_id}[/cyan]  "
            f"prefix=[cyan]{EXTRACT_PREFIX}/[/cyan]  "
            f"snapshot_date=[cyan]{snapshot.isoformat() if snapshot else 'all'}[/cyan]  "
            f"dry_run=[cyan]{dry_run}[/cyan]",
            border_style="blue",
        )
    )

    client = bigquery.Client(project=gcp.project_id, location=gcp.location)
    grouped = _fetch_by_snapshot(client, table_id, snapshot)
    if not grouped:
        console.print("[yellow]No extract rows matched; nothing to publish.[/yellow]")
        raise typer.Exit(code=0)

    payloads = [
        build_payload(
            records,
            snapshot_date=snap,
            partition_label=PARTITION_LABEL,
            dataset_prefix=EXTRACT_PREFIX,
            # These records already carry their own keys and timestamps. Adding
            # a fresh ingested_at would change the bytes on every run and defeat
            # the point of exporting the whole table idempotently.
            decorate_with_metadata=False,
        )
        for snap, records in sorted(grouped.items())
    ]

    if dry_run:
        for payload in payloads:
            console.print(
                f"  would write [cyan]{payload.blob_key}[/cyan]  "
                f"({payload.row_count:,} rows, {payload.size_bytes:,} B)"
            )
        raise typer.Exit(code=0)

    gcs = GcsRawUploader()
    s3 = S3RawUploader()
    results: dict[str, tuple[str, str]] = {}
    for payload in payloads:
        gcs_result = gcs.upload(payload)
        s3_result = s3.upload(payload)
        results[payload.blob_key] = (gcs_result.uri, s3_result.uri)
        console.print(f"  published [cyan]{payload.blob_key}[/cyan]")

    console.print("\n", _render(payloads, results))

    total_rows = sum(p.row_count for p in payloads)
    console.print(
        Panel.fit(
            f"[bold green]Published {total_rows:,} extract rows across "
            f"{len(payloads)} snapshot date(s) to both landing zones.[/bold green]",
            border_style="green",
        )
    )
    sys.exit(0)


if __name__ == "__main__":
    app()
