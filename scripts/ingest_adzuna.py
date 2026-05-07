"""End-to-end Adzuna ingestion orchestrator: API -> GCS -> BigQuery."""

from __future__ import annotations

import logging
import sys
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Final

import typer
from dotenv import load_dotenv
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

PROJECT_ROOT: Final[Path] = Path(__file__).resolve().parent.parent
load_dotenv(PROJECT_ROOT / ".env")

# Module imports must follow load_dotenv so config classes pick up env values.
from adzuna_pipeline.client import (  # noqa: E402
    AdzunaApiError, AdzunaClient, SearchQuery,
)
from adzuna_pipeline.loader import BigQueryRawLoader  # noqa: E402
from adzuna_pipeline.storage import GcsRawUploader, UploadResult  # noqa: E402

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
    upload: UploadResult | None
    rows_loaded: int


def _configure_logging(level: str) -> None:
    """Configure root logging and suppress noisy third-party loggers.

    httpx logs each request URL at INFO; for Adzuna the URL contains the
    application key as a query parameter, so we cap httpx at WARNING to keep
    credentials out of stdout and log files.
    """
    logging.basicConfig(
        level=level.upper(),
        format="%(asctime)s | %(levelname)-7s | %(name)s | %(message)s",
    )
    for noisy in ("httpx", "httpcore", "google.auth", "urllib3"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def _parse_cities(value: str) -> list[str]:
    return [c.strip() for c in value.split(",") if c.strip()]


def _parse_snapshot_date(value: str | None) -> date:
    if not value:
        return datetime.now(tz=timezone.utc).date()
    return date.fromisoformat(value)


def _render_summary(reports: list[CityIngestReport], snapshot: date) -> Table:
    table = Table(title=f"Ingestion summary | snapshot_date={snapshot.isoformat()}")
    table.add_column("City", style="cyan")
    table.add_column("Adzuna total", justify="right")
    table.add_column("Fetched", justify="right")
    table.add_column("GCS object", overflow="fold")
    table.add_column("BQ rows loaded", justify="right", style="magenta")
    for report in reports:
        gcs_uri = report.upload.gcs_uri if report.upload else "(skipped)"
        table.add_row(
            report.city,
            f"{report.total_count:,}",
            f"{report.fetched_rows:,}",
            gcs_uri,
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
        help="Fetch and upload to GCS but skip the BigQuery load step.",
    ),
    replace_partition: bool = typer.Option(
        False,
        "--replace-partition",
        help="Delete the snapshot_date partition before loading to ensure idempotency.",
    ),
    log_level: str = typer.Option("INFO", help="Logging level."),
) -> None:
    """Run the Adzuna -> GCS -> BigQuery ingestion for one snapshot date."""
    _configure_logging(log_level)
    target_cities = _parse_cities(cities)
    if not target_cities:
        console.print("[red]No cities specified.[/red]")
        raise typer.Exit(code=2)

    snapshot = _parse_snapshot_date(snapshot_date)
    console.print(Panel.fit(
        f"[bold]Adzuna ingestion[/bold]\n"
        f"snapshot_date=[cyan]{snapshot.isoformat()}[/cyan]  "
        f"cities=[cyan]{', '.join(target_cities)}[/cyan]  "
        f"category=[cyan]{category}[/cyan]  "
        f"max_pages=[cyan]{max_pages}[/cyan]  "
        f"max_days_old=[cyan]{max_days_old}[/cyan]  "
        f"dry_run=[cyan]{dry_run}[/cyan]",
        border_style="blue",
    ))

    uploader = GcsRawUploader()
    loader: BigQueryRawLoader | None = None
    if not dry_run:
        loader = BigQueryRawLoader()
        loader.ensure_table()
        if replace_partition:
            loader.delete_partition(snapshot.isoformat())

    reports: list[CityIngestReport] = []
    exit_code = 0

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
                exit_code = 1
                reports.append(CityIngestReport(city, 0, 0, None, 0))
                continue
            console.print(f"  fetched {len(rows):,} rows  (Adzuna total {total_count:,})")

            if not rows:
                reports.append(CityIngestReport(city, total_count, 0, None, 0))
                continue

            upload = uploader.upload_jsonl(
                rows,
                snapshot_date=snapshot,
                partition_label=city,
            )
            console.print(f"  uploaded -> {upload.gcs_uri}  ({upload.size_bytes:,} bytes)")

            rows_loaded = 0
            if loader is not None:
                load_result = loader.load_from_gcs(upload.gcs_uri)
                rows_loaded = load_result.rows_loaded
                console.print(
                    f"  loaded {rows_loaded:,} rows into {load_result.table_id}  "
                    f"(job {load_result.job_id})"
                )

            reports.append(CityIngestReport(
                city=city,
                total_count=total_count,
                fetched_rows=len(rows),
                upload=upload,
                rows_loaded=rows_loaded,
            ))

    console.print("\n", _render_summary(reports, snapshot))

    if exit_code == 0:
        total_loaded = sum(r.rows_loaded for r in reports)
        total_fetched = sum(r.fetched_rows for r in reports)
        message = (
            f"Ingestion complete — fetched {total_fetched:,} rows, "
            f"loaded {total_loaded:,} rows into BigQuery."
            if not dry_run else
            f"Dry run complete — fetched {total_fetched:,} rows, "
            f"uploaded to GCS only."
        )
        console.print(Panel.fit(f"[bold green]{message}[/bold green]", border_style="green"))
    else:
        console.print(Panel.fit(
            "[bold red]One or more cities failed; see logs above.[/bold red]",
            border_style="red",
        ))

    sys.exit(exit_code)


if __name__ == "__main__":
    app()
