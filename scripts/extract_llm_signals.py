"""End-to-end LLM extraction: read pending from BigQuery, call Qwen, upsert results."""

from __future__ import annotations

import asyncio
import logging
import sys
from datetime import date
from pathlib import Path
from typing import Final

import typer
from dotenv import load_dotenv
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

PROJECT_ROOT: Final[Path] = Path(__file__).resolve().parent.parent
load_dotenv(PROJECT_ROOT / ".env")

from adzuna_pipeline.extraction.batch import ExtractionBatchProcessor  # noqa: E402

app = typer.Typer(add_completion=False, help="LLM extraction pipeline.")
console = Console()


def _configure_logging(level: str) -> None:
    logging.basicConfig(
        level=level.upper(),
        format="%(asctime)s | %(levelname)-7s | %(name)s | %(message)s",
    )
    for noisy in ("httpx", "httpcore", "google.auth", "urllib3"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


@app.command()
def main(
    sample_size: int = typer.Option(
        0,
        help="Pending-row cap. 0 means process every job that lacks an extract.",
    ),
    concurrency: int = typer.Option(
        5,
        help="Maximum simultaneous in-flight Qwen requests.",
    ),
    snapshot_date: str = typer.Option(
        "",
        help="Restrict to one snapshot_date (YYYY-MM-DD).",
    ),
    dry_run: bool = typer.Option(
        False,
        "--dry-run",
        help="Fetch the pending set and exit without calling the LLM.",
    ),
    log_level: str = typer.Option("INFO", help="Logging level."),
) -> None:
    """Extract structured signals from pending Adzuna jobs and upsert to BigQuery."""
    _configure_logging(log_level)
    snap = date.fromisoformat(snapshot_date) if snapshot_date else None

    processor = ExtractionBatchProcessor()
    processor.ensure_table()

    console.print(Panel.fit(
        f"[bold]LLM extraction[/bold]\n"
        f"sample_size=[cyan]{sample_size or 'all pending'}[/cyan]  "
        f"concurrency=[cyan]{concurrency}[/cyan]  "
        f"snapshot_date=[cyan]{snap.isoformat() if snap else 'any'}[/cyan]  "
        f"dry_run=[cyan]{dry_run}[/cyan]",
        border_style="blue",
    ))

    pending = processor.fetch_pending(snapshot_date=snap, sample_size=sample_size)
    console.print(f"[bold]Pending jobs:[/bold] {len(pending):,}")

    if not pending:
        console.print("[green]Nothing to do — all jobs already have extracts.[/green]")
        sys.exit(0)

    if dry_run:
        console.print("[yellow]Dry run: stopping before LLM calls.[/yellow]")
        sys.exit(0)

    records = asyncio.run(processor.run_async(pending, concurrency=concurrency))
    ok = sum(1 for r in records if r.extraction_status == "ok")
    err = len(records) - ok
    upserted = processor.merge_results(records)

    summary = Table(title=f"Extraction summary | {processor.table_id}")
    summary.add_column("Metric", style="cyan")
    summary.add_column("Value", justify="right", style="magenta")
    summary.add_row("Pending fetched", f"{len(pending):,}")
    summary.add_row("Extraction OK", f"{ok:,}")
    summary.add_row("Extraction failed", f"{err:,}")
    summary.add_row("Upserted to BigQuery", f"{upserted:,}")
    console.print("\n", summary)

    sys.exit(0 if err == 0 else 1)


if __name__ == "__main__":
    app()
