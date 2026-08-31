"""Run every equivalence check against all three engines and report disagreement.

ADR-0002's claim is that BigQuery, Athena and Snowflake return the same answers
over the same data. This makes that claim executable: the numbers quoted in the
READMEs are regenerated here rather than transcribed, and a divergence fails
loudly with the rows that differ.

It is also the cost comparison the ADR promised. BigQuery and Athena both
report bytes scanned per query; Snowflake bills credits and does not expose
them per query without ACCOUNTADMIN, so its column is blank by design rather
than by omission.

Needs credentials for all three clouds. Exit code is non-zero if any check
disagrees, so it can gate a release even though it cannot run in the hermetic
CI gate.
"""

from __future__ import annotations

import sys

import typer
from dotenv import load_dotenv
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from adzuna_pipeline.cli import PROJECT_ROOT, configure_logging

load_dotenv(PROJECT_ROOT / ".env")

# Module imports must follow load_dotenv so config classes pick up env values.
from adzuna_pipeline.config import get_gcp  # noqa: E402
from adzuna_pipeline.engines import (  # noqa: E402
    AthenaEngine,
    BigQueryEngine,
    QueryEngine,
    QueryResult,
    SnowflakeEngine,
)
from adzuna_pipeline.equivalence import CHECKS, EquivalenceCheck  # noqa: E402

app = typer.Typer(add_completion=False, help="Verify the three engines agree.")
console = Console()


def _sql_for(check: EquivalenceCheck, engine_name: str, project: str) -> str:
    sql = {
        "bigquery": check.bigquery,
        "athena": check.athena,
        "snowflake": check.snowflake,
    }[engine_name]
    return sql.format(project=project).strip()


def _format_bytes(value: int | None) -> str:
    return "—" if value is None else f"{value:,}"


def _render_costs(costs: dict[str, list[int]]) -> Table:
    """Summarise what each engine reported scanning across the whole run."""
    table = Table(title="Scan cost reported per engine")
    table.add_column("Engine")
    table.add_column("Queries", justify="right")
    table.add_column("Total scanned", justify="right")
    table.add_column("Billing model")
    models = {
        "bigquery": "per byte scanned, 1 TiB/month free",
        "athena": "per byte scanned, $5/TB, 10 MB minimum per query",
        "snowflake": "per credit-second of warehouse uptime, not per byte",
    }
    for name, values in costs.items():
        reported = [v for v in values if v is not None]
        total = f"{sum(reported):,} B" if reported else "not exposed per query"
        table.add_row(name, str(len(values)), total, models[name])
    return table


@app.command()
def main(
    only: str = typer.Option("", help="Run just the checks whose name contains this text."),
    verbose: bool = typer.Option(False, "--verbose", help="Print rows even when they agree."),
    log_level: str = typer.Option("WARNING", help="Logging level."),
) -> None:
    """Execute the equivalence checks and compare the results."""
    configure_logging(log_level)
    project = get_gcp().project_id

    checks = [c for c in CHECKS if only.lower() in c.name.lower()] if only else list(CHECKS)
    if not checks:
        console.print(f"[yellow]No check matches {only!r}.[/yellow]")
        raise typer.Exit(code=0)

    console.print(
        Panel.fit(
            f"[bold]Cross-engine equivalence[/bold]\n"
            f"checks=[cyan]{len(checks)}[/cyan]  "
            f"engines=[cyan]bigquery, athena, snowflake[/cyan]",
            border_style="blue",
        )
    )

    engines: list[QueryEngine] = [BigQueryEngine(), AthenaEngine(), SnowflakeEngine()]
    costs: dict[str, list[int]] = {e.name: [] for e in engines}
    disagreements: list[str] = []

    summary = Table(title="Equivalence checks")
    summary.add_column("Check", overflow="fold")
    summary.add_column("Rows", justify="right")
    summary.add_column("Agree")
    summary.add_column("BigQuery scanned", justify="right")
    summary.add_column("Athena scanned", justify="right")

    try:
        for check in checks:
            results: dict[str, QueryResult] = {}
            failed: str | None = None
            for engine in engines:
                try:
                    results[engine.name] = engine.run(_sql_for(check, engine.name, project))
                except Exception as exc:  # noqa: BLE001 - any engine error is a disagreement
                    failed = f"{engine.name}: {exc}"
                    break

            if failed is not None:
                disagreements.append(f"{check.name} — {failed}")
                summary.add_row(check.name, "—", "[red]ERROR[/red]", "—", "—")
                continue

            for name, result in results.items():
                if result.scanned_bytes is not None:
                    costs[name].append(result.scanned_bytes)

            rows = [r.rows for r in results.values()]
            agree = all(r == rows[0] for r in rows)
            if not agree:
                disagreements.append(check.name)

            summary.add_row(
                check.name,
                str(len(rows[0])),
                "[green]yes[/green]" if agree else "[red]NO[/red]",
                _format_bytes(results["bigquery"].scanned_bytes),
                _format_bytes(results["athena"].scanned_bytes),
            )

            if verbose or not agree:
                console.print(f"\n[bold]{check.name}[/bold]")
                console.print(f"[dim]{check.why}[/dim]")
                for name, result in results.items():
                    console.print(f"  {name:<10} {result.rows}")
    finally:
        for engine in engines:
            engine.close()

    console.print("\n", summary)
    console.print("\n", _render_costs(costs))

    if disagreements:
        console.print(
            Panel.fit(
                "[bold red]Engines disagree on:[/bold red]\n  " + "\n  ".join(disagreements),
                border_style="red",
            )
        )
        sys.exit(1)

    console.print(
        Panel.fit(
            f"[bold green]All {len(checks)} checks agree across the three engines.[/bold green]",
            border_style="green",
        )
    )
    sys.exit(0)


if __name__ == "__main__":
    app()
