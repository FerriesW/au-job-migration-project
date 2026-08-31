"""End-to-end Adzuna ingestion orchestrator: API -> GCS + S3 -> BigQuery."""

from __future__ import annotations

import sys
from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import Final

import typer
from dotenv import load_dotenv
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from adzuna_pipeline.cli import PROJECT_ROOT, configure_logging

load_dotenv(PROJECT_ROOT / ".env")

# Module imports must follow load_dotenv so config classes pick up env values.
from botocore.exceptions import BotoCoreError, ClientError  # noqa: E402
from pydantic import ValidationError  # noqa: E402

from adzuna_pipeline.client import (  # noqa: E402
    AdzunaApiError,
    AdzunaClient,
    SearchQuery,
)
from adzuna_pipeline.loader import BigQueryRawLoader  # noqa: E402
from adzuna_pipeline.storage import (  # noqa: E402
    GcsRawUploader,
    S3RawUploader,
    UploadResult,
    build_payload,
)

DEFAULT_CITIES: Final[tuple[str, ...]] = ("Melbourne", "Sydney", "Brisbane")
DEFAULT_CATEGORY: Final[str] = "it-jobs"
DEFAULT_MAX_PAGES: Final[int] = 60
DEFAULT_MAX_DAYS_OLD: Final[int] = 30

app = typer.Typer(add_completion=False, help="Adzuna ingestion pipeline.")
console = Console()


@dataclass
class CityIngestReport:
    """Per-city ingestion outcome captured for the summary table."""

    city: str
    total_count: int
    fetched_rows: int
    rows_loaded: int = 0
    gcs_upload: UploadResult | None = None
    s3_upload: UploadResult | None = None
    s3_error: str | None = None


def _parse_cities(value: str) -> list[str]:
    return [c.strip() for c in value.split(",") if c.strip()]


def _parse_snapshot_date(value: str | None) -> date:
    if not value:
        return datetime.now(tz=UTC).date()
    return date.fromisoformat(value)


def _build_s3_uploader() -> S3RawUploader | None:
    """Construct the S3 uploader, or return None if AWS is not configured.

    A missing S3_BUCKET_RAW is treated as "this machine only has GCP set up"
    rather than an error, so the GCP path stays runnable for anyone who has
    not provisioned the AWS side.
    """
    try:
        return S3RawUploader()
    except (BotoCoreError, ClientError, ValidationError) as exc:
        console.print(f"[yellow]S3 mirror disabled — AWS not configured:[/yellow] {exc}")
        return None


def _render_summary(reports: list[CityIngestReport], snapshot: date) -> Table:
    """Render the per-city summary.

    Both landing zones write the same object key, so the key is shown once and
    each cloud gets its own status column instead of a second long URI.
    """
    table = Table(title=f"Ingestion summary | snapshot_date={snapshot.isoformat()}")
    table.add_column("City", style="cyan")
    table.add_column("Adzuna total", justify="right")
    table.add_column("Fetched", justify="right")
    table.add_column("Object key", overflow="fold")
    table.add_column("GCS", justify="center")
    table.add_column("S3", justify="center")
    table.add_column("BQ rows loaded", justify="right", style="magenta")
    for report in reports:
        if report.gcs_upload is None:
            object_key = "—"
            gcs_status = "—"
        else:
            object_key = report.gcs_upload.uri.split("/", 3)[-1]
            gcs_status = "[green]ok[/green]"

        if report.s3_error is not None:
            s3_status = "[red]FAILED[/red]"
        elif report.s3_upload is not None:
            s3_status = "[green]ok[/green]"
        else:
            s3_status = "—"

        table.add_row(
            report.city,
            f"{report.total_count:,}",
            f"{report.fetched_rows:,}",
            object_key,
            gcs_status,
            s3_status,
            f"{report.rows_loaded:,}",
        )
    return table


@app.command()
def main(
    cities: str = typer.Option(
        ",".join(DEFAULT_CITIES),
        help="Comma-separated city names to ingest.",
    ),
    category: str = typer.Option(DEFAULT_CATEGORY, help="Adzuna category slug."),
    max_days_old: int = typer.Option(DEFAULT_MAX_DAYS_OLD, help="Freshness window in days."),
    max_pages: int = typer.Option(DEFAULT_MAX_PAGES, help="Pagination cap per city."),
    snapshot_date: str = typer.Option(
        "",
        help="Logical partition date (YYYY-MM-DD); defaults to today UTC.",
    ),
    country: str = typer.Option("au", help="Adzuna country code."),
    dry_run: bool = typer.Option(
        False,
        "--dry-run",
        help="Fetch and land in both object stores but skip the BigQuery load step.",
    ),
    replace_partition: bool = typer.Option(
        False,
        "--replace-partition",
        help="Delete the snapshot_date partition before loading to ensure idempotency.",
    ),
    skip_s3: bool = typer.Option(
        False,
        "--skip-s3",
        help="Write only the GCS landing zone. The S3 mirror is left untouched.",
    ),
    log_level: str = typer.Option("INFO", help="Logging level."),
) -> None:
    """Run the Adzuna ingestion for one snapshot date.

    Each snapshot is serialised once and written to both landing zones — GCS
    and S3 — before BigQuery loads it from GCS. S3 is the secondary target:
    a failure there is reported and reflected in the exit code, but never
    interrupts the GCP path that Power BI and the scheduled dbt build depend
    on. Re-running ingest for the same snapshot date repairs a missed mirror,
    because every upload overwrites its key.
    """
    configure_logging(log_level)
    target_cities = _parse_cities(cities)
    if not target_cities:
        console.print("[red]No cities specified.[/red]")
        raise typer.Exit(code=2)

    snapshot = _parse_snapshot_date(snapshot_date)
    console.print(
        Panel.fit(
            f"[bold]Adzuna ingestion[/bold]\n"
            f"snapshot_date=[cyan]{snapshot.isoformat()}[/cyan]  "
            f"cities=[cyan]{', '.join(target_cities)}[/cyan]  "
            f"category=[cyan]{category}[/cyan]  "
            f"max_pages=[cyan]{max_pages}[/cyan]  "
            f"max_days_old=[cyan]{max_days_old}[/cyan]  "
            f"dry_run=[cyan]{dry_run}[/cyan]",
            border_style="blue",
        )
    )

    gcs_uploader = GcsRawUploader()
    s3_uploader = None if skip_s3 else _build_s3_uploader()
    loader: BigQueryRawLoader | None = None
    if not dry_run:
        loader = BigQueryRawLoader()
        loader.ensure_table()
        if replace_partition:
            loader.delete_partition(snapshot.isoformat())

    reports: list[CityIngestReport] = []
    fetch_failures: list[str] = []

    with AdzunaClient() as client:
        for city in target_cities:
            console.print(f"\n[bold]→ {city}[/bold]")
            query = SearchQuery(
                country=country,
                where=city,
                category=category or None,
                max_days_old=max_days_old,
            )

            try:
                total_count, rows = client.collect(query, max_pages=max_pages)
            except AdzunaApiError as exc:
                console.print(f"[red]Adzuna fetch failed for {city}:[/red] {exc}")
                fetch_failures.append(city)
                reports.append(CityIngestReport(city, 0, 0))
                continue
            console.print(f"  fetched {len(rows):,} rows  (Adzuna total {total_count:,})")

            if not rows:
                reports.append(CityIngestReport(city, total_count, 0))
                continue

            # Serialise once so both landing zones receive identical bytes;
            # re-serialising per cloud would stamp a different ingested_at.
            payload = build_payload(rows, snapshot_date=snapshot, partition_label=city)

            gcs_upload = gcs_uploader.upload(payload)
            console.print(f"  uploaded -> {gcs_upload.uri}  ({gcs_upload.size_bytes:,} bytes)")

            s3_upload: UploadResult | None = None
            s3_error: str | None = None
            if s3_uploader is not None:
                try:
                    s3_upload = s3_uploader.upload(payload)
                    console.print(f"  mirrored -> {s3_upload.uri}")
                except (BotoCoreError, ClientError) as exc:
                    # Deliberately non-fatal: the GCP path feeds Power BI and the
                    # scheduled dbt build, and must not stop because the mirror failed.
                    s3_error = str(exc)
                    console.print(f"  [yellow]S3 mirror failed:[/yellow] {exc}")

            rows_loaded = 0
            if loader is not None:
                load_result = loader.load_from_gcs(gcs_upload.uri)
                rows_loaded = load_result.rows_loaded
                console.print(
                    f"  loaded {rows_loaded:,} rows into {load_result.table_id}  "
                    f"(job {load_result.job_id})"
                )

            reports.append(
                CityIngestReport(
                    city=city,
                    total_count=total_count,
                    fetched_rows=len(rows),
                    rows_loaded=rows_loaded,
                    gcs_upload=gcs_upload,
                    s3_upload=s3_upload,
                    s3_error=s3_error,
                )
            )

    console.print("\n", _render_summary(reports, snapshot))

    mirror_failures = [r.city for r in reports if r.s3_error is not None]

    if fetch_failures:
        console.print(
            Panel.fit(
                f"[bold red]Adzuna fetch failed for {', '.join(fetch_failures)}; "
                f"see logs above.[/bold red]",
                border_style="red",
            )
        )
    elif mirror_failures:
        # The GCP path completed. Only the S3 mirror is behind, and re-running
        # ingest for this snapshot date overwrites and repairs it.
        console.print(
            Panel.fit(
                f"[bold yellow]GCP path complete; S3 mirror missing for "
                f"{', '.join(mirror_failures)}. Re-run to repair.[/bold yellow]",
                border_style="yellow",
            )
        )
    else:
        total_loaded = sum(r.rows_loaded for r in reports)
        total_fetched = sum(r.fetched_rows for r in reports)
        landed = "GCS only" if s3_uploader is None else "GCS and S3"
        message = (
            f"Ingestion complete — fetched {total_fetched:,} rows, landed in {landed}, "
            f"loaded {total_loaded:,} rows into BigQuery."
            if not dry_run
            else f"Dry run complete — fetched {total_fetched:,} rows, landed in {landed}."
        )
        console.print(Panel.fit(f"[bold green]{message}[/bold green]", border_style="green"))

    sys.exit(1 if fetch_failures or mirror_failures else 0)


if __name__ == "__main__":
    app()
