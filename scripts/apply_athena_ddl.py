"""Apply the versioned Glue catalog DDL in `aws/glue/` through Athena.

The Glue Data Catalog is defined by SQL files in the repository rather than by
a crawler or by clicking in the console, so the JSON-to-table mapping is
reviewable in a diff and reproducible from a clean account. Each file holds one
statement — Athena executes a single statement per call — and files are applied
in filename order.

Submitting, polling and fetching live in `adzuna_pipeline.engines`; this script
only decides what to run and how to report it.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Final

import typer
from dotenv import load_dotenv
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from adzuna_pipeline.cli import PROJECT_ROOT, configure_logging

load_dotenv(PROJECT_ROOT / ".env")

# Module imports must follow load_dotenv so config classes pick up env values.
from adzuna_pipeline.engines import AthenaEngine  # noqa: E402

DEFAULT_DDL_DIR: Final[Path] = PROJECT_ROOT / "aws" / "glue"

VERIFY_SQL: Final[str] = """
select 'adzuna_jobs' as table_name, snapshot_date, count(*) as rows
from au_jobs_radar.adzuna_jobs group by 1, 2
union all
select 'adzuna_llm_extract', snapshot_date, count(*)
from au_jobs_radar.adzuna_llm_extract group by 1, 2
order by 1, 2
"""

app = typer.Typer(add_completion=False, help="Apply Glue catalog DDL through Athena.")
console = Console()


@dataclass(frozen=True)
class Outcome:
    """Result of applying one DDL file."""

    label: str
    ok: bool
    detail: str


def _render(outcomes: list[Outcome]) -> Table:
    table = Table(title="Athena DDL application")
    table.add_column("Statement", overflow="fold")
    table.add_column("State")
    table.add_column("Detail", overflow="fold")
    for o in outcomes:
        state = "[green]SUCCEEDED[/green]" if o.ok else "[red]FAILED[/red]"
        table.add_row(o.label, state, o.detail)
    return table


@app.command()
def main(
    ddl_dir: str = typer.Option(str(DEFAULT_DDL_DIR), help="Directory of .sql files to apply."),
    verify: bool = typer.Option(
        False,
        "--verify",
        help="After applying, count rows per table and snapshot date.",
    ),
    dry_run: bool = typer.Option(
        False,
        "--dry-run",
        help="List the statements that would be applied without executing them.",
    ),
    log_level: str = typer.Option("INFO", help="Logging level."),
) -> None:
    """Apply every .sql file in ``ddl_dir``, in filename order."""
    configure_logging(log_level)

    ddl_path = Path(ddl_dir)
    files = sorted(ddl_path.glob("*.sql"))
    if not files:
        console.print(f"[yellow]No .sql files under {ddl_path}.[/yellow]")
        raise typer.Exit(code=0)

    console.print(
        Panel.fit(
            f"[bold]Glue catalog DDL[/bold]\n"
            f"source=[cyan]{ddl_path}[/cyan]  files=[cyan]{len(files)}[/cyan]  "
            f"dry_run=[cyan]{dry_run}[/cyan]",
            border_style="blue",
        )
    )

    if dry_run:
        for path in files:
            console.print(f"  would apply [cyan]{path.name}[/cyan]")
        raise typer.Exit(code=0)

    engine = AthenaEngine()
    outcomes: list[Outcome] = []
    try:
        for path in files:
            sql = path.read_text(encoding="utf-8").strip().rstrip(";")
            try:
                result = engine.run(sql)
                outcomes.append(Outcome(path.name, True, f"{result.elapsed_ms or 0:,} ms"))
            except Exception as exc:  # noqa: BLE001 - report and stop, whatever failed
                outcomes.append(Outcome(path.name, False, str(exc)))
                console.print(f"[red]{path.name} failed:[/red] {exc}")
                break

        console.print("\n", _render(outcomes))

        if any(not o.ok for o in outcomes):
            raise typer.Exit(code=1)

        if verify:
            console.print("\n[bold]Verification — rows per table and snapshot date[/bold]")
            result = engine.run(VERIFY_SQL)
            table = Table()
            for header in ("table", "snapshot_date", "rows"):
                table.add_column(header)
            for row in result.rows:
                table.add_row(*[cell or "" for cell in row])
            console.print(table)
    finally:
        engine.close()

    console.print(
        Panel.fit(
            f"[bold green]Applied {len(outcomes)} statement(s) to the Glue catalog.[/bold green]",
            border_style="green",
        )
    )


if __name__ == "__main__":
    app()
