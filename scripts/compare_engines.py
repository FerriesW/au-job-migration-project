"""Run every equivalence check against the reachable engines and report disagreement.

ADR-0002's claim is that BigQuery, Athena and Snowflake return the same answers
over the same data. This makes that claim executable: the numbers quoted in the
READMEs are regenerated here rather than transcribed, and a divergence fails
loudly with the rows that differ.

It is also the cost comparison the ADR promised. BigQuery and Athena both
report bytes scanned per query; Snowflake bills credits and does not expose
them per query without ACCOUNTADMIN, so its column is blank by design rather
than by omission.

**An engine being unreachable is not a disagreement.** They are separate
findings and the script reports them separately: unreachable engines are probed
once up front, named in the output, and excluded from the comparison, while the
exit code stays zero as long as the engines that did answer agree. A transient
Athena outage — or the Snowflake trial expiring — should not be reported as a
modelling defect, because someone would then go looking for a data problem that
does not exist.

Degradation has a floor. Fewer than two reachable engines means there is
nothing to compare, and that exits 2 rather than passing quietly.

Needs credentials for the clouds it is asked to reach. Exit codes: 0 all
compared engines agree, 1 a real disagreement, 2 too few engines to compare.
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

MINIMUM_ENGINES: int = 2
PROBE_SQL: dict[str, str] = {
    "bigquery": "select 1",
    "athena": "select 1",
    "snowflake": "select 1",
}

app = typer.Typer(add_completion=False, help="Verify the reachable engines agree.")
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


def _probe(engines: list[QueryEngine]) -> tuple[list[QueryEngine], dict[str, str]]:
    """Split the engines into those that answer and those that do not.

    Constructing an engine can fail on missing configuration and running can
    fail on expired credentials, so both are attempted here — once, up front —
    rather than discovered separately inside every check.
    """
    reachable: list[QueryEngine] = []
    unreachable: dict[str, str] = {}
    for engine in engines:
        try:
            engine.run(PROBE_SQL[engine.name])
            reachable.append(engine)
        except Exception as exc:  # noqa: BLE001 - any failure to answer is unreachable
            unreachable[engine.name] = str(exc).splitlines()[0][:160]
    return reachable, unreachable


def _build_engines() -> tuple[list[QueryEngine], dict[str, str]]:
    """Construct each engine, recording the ones that cannot even be built."""
    built: list[QueryEngine] = []
    failed: dict[str, str] = {}
    for name, factory in (
        ("bigquery", BigQueryEngine),
        ("athena", AthenaEngine),
        ("snowflake", SnowflakeEngine),
    ):
        try:
            built.append(factory())
        except Exception as exc:  # noqa: BLE001 - unconfigured is just unreachable
            failed[name] = str(exc).splitlines()[0][:160]
    return built, failed


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
        total = f"{sum(values):,} B" if values else "not exposed per query"
        table.add_row(name, str(len(values)), total, models[name])
    return table


@app.command()
def main(
    only: str = typer.Option("", help="Run just the checks whose name contains this text."),
    verbose: bool = typer.Option(False, "--verbose", help="Print rows even when they agree."),
    require_all: bool = typer.Option(
        False,
        "--require-all",
        help="Treat an unreachable engine as a failure. Use before a release.",
    ),
    log_level: str = typer.Option("WARNING", help="Logging level."),
) -> None:
    """Execute the equivalence checks against whichever engines answer."""
    configure_logging(log_level)
    project = get_gcp().project_id

    checks = [c for c in CHECKS if only.lower() in c.name.lower()] if only else list(CHECKS)
    if not checks:
        console.print(f"[yellow]No check matches {only!r}.[/yellow]")
        raise typer.Exit(code=0)

    built, unbuildable = _build_engines()
    engines, unresponsive = _probe(built)
    unreachable = {**unbuildable, **unresponsive}

    console.print(
        Panel.fit(
            f"[bold]Cross-engine equivalence[/bold]\n"
            f"checks=[cyan]{len(checks)}[/cyan]  "
            f"comparing=[cyan]{', '.join(e.name for e in engines) or 'nothing'}[/cyan]",
            border_style="blue",
        )
    )

    for name, reason in unreachable.items():
        console.print(f"[yellow]skipped {name} — unreachable:[/yellow] {reason}")

    if len(engines) < MINIMUM_ENGINES:
        console.print(
            Panel.fit(
                f"[bold red]Only {len(engines)} engine(s) reachable; at least "
                f"{MINIMUM_ENGINES} are needed to compare anything.[/bold red]",
                border_style="red",
            )
        )
        for engine in engines:
            engine.close()
        sys.exit(2)

    costs: dict[str, list[int]] = {e.name: [] for e in engines}
    disagreements: list[str] = []
    errors: list[str] = []

    summary = Table(title="Equivalence checks")
    summary.add_column("Check", overflow="fold")
    summary.add_column("Rows", justify="right")
    summary.add_column("Agree")
    for engine in engines:
        summary.add_column(f"{engine.name} scanned", justify="right")

    try:
        for check in checks:
            results: dict[str, QueryResult] = {}
            failure: str | None = None
            for engine in engines:
                try:
                    results[engine.name] = engine.run(_sql_for(check, engine.name, project))
                except Exception as exc:  # noqa: BLE001 - a query error is a real failure
                    failure = f"{engine.name}: {str(exc).splitlines()[0][:160]}"
                    break

            if failure is not None:
                # The engine answered the probe but failed this query, so this
                # is a fault in the check or the data, not an absent engine.
                errors.append(f"{check.name} — {failure}")
                summary.add_row(check.name, "—", "[red]ERROR[/red]", *["—"] * len(engines))
                continue

            for name, result in results.items():
                if result.scanned_bytes is not None:
                    costs[name].append(result.scanned_bytes)

            rows = [results[e.name].rows for e in engines]
            agree = all(r == rows[0] for r in rows)
            if not agree:
                disagreements.append(check.name)

            summary.add_row(
                check.name,
                str(len(rows[0])),
                "[green]yes[/green]" if agree else "[red]NO[/red]",
                *[_format_bytes(results[e.name].scanned_bytes) for e in engines],
            )

            if verbose or not agree:
                console.print(f"\n[bold]{check.name}[/bold]")
                console.print(f"[dim]{check.why}[/dim]")
                for engine in engines:
                    console.print(f"  {engine.name:<10} {results[engine.name].rows}")
    finally:
        for engine in engines:
            engine.close()

    console.print("\n", summary)
    console.print("\n", _render_costs(costs))

    if disagreements or errors:
        detail = [f"disagreed: {c}" for c in disagreements] + [f"errored: {e}" for e in errors]
        console.print(
            Panel.fit(
                "[bold red]Not all checks passed:[/bold red]\n  " + "\n  ".join(detail),
                border_style="red",
            )
        )
        sys.exit(1)

    if unreachable and require_all:
        console.print(
            Panel.fit(
                f"[bold red]All {len(checks)} checks agree across "
                f"{len(engines)} engine(s), but --require-all was given and "
                f"{', '.join(unreachable)} could not be reached.[/bold red]",
                border_style="red",
            )
        )
        sys.exit(1)

    skipped = f"  Skipped: {', '.join(unreachable)}." if unreachable else ""
    console.print(
        Panel.fit(
            f"[bold green]All {len(checks)} checks agree across {len(engines)} of "
            f"{len(engines) + len(unreachable)} engines.{skipped}[/bold green]",
            border_style="green",
        )
    )
    sys.exit(0)


if __name__ == "__main__":
    app()
