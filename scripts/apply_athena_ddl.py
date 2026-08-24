"""Apply the versioned Glue catalog DDL in `aws/glue/` through Athena.

The Glue Data Catalog is defined by SQL files in the repository rather than by
a crawler or by clicking in the console, so the JSON-to-table mapping is
reviewable in a diff and reproducible from a clean account. Each file holds one
statement — Athena executes a single statement per call — and files are applied
in filename order.
"""

from __future__ import annotations

import logging
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

import typer
from dotenv import load_dotenv
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

PROJECT_ROOT: Final[Path] = Path(__file__).resolve().parent.parent
load_dotenv(PROJECT_ROOT / ".env")

# Module imports must follow load_dotenv so config classes pick up env values.
import boto3  # noqa: E402
from botocore.exceptions import BotoCoreError, ClientError  # noqa: E402

from adzuna_pipeline.config import get_aws  # noqa: E402

LOGGER: Final[logging.Logger] = logging.getLogger(__name__)

DEFAULT_DDL_DIR: Final[Path] = PROJECT_ROOT / "aws" / "glue"
POLL_SECONDS: Final[float] = 1.0
POLL_TIMEOUT_SECONDS: Final[float] = 120.0
TERMINAL_STATES: Final[frozenset[str]] = frozenset({"SUCCEEDED", "FAILED", "CANCELLED"})

VERIFY_SQL: Final[str] = """
select snapshot_date,
       count(*)                        as jobs,
       count(distinct source_city)     as cities,
       min(location.display_name)      as sample_location
from au_jobs_radar.adzuna_jobs
group by snapshot_date
order by snapshot_date
"""

app = typer.Typer(add_completion=False, help="Apply Glue catalog DDL through Athena.")
console = Console()


@dataclass(frozen=True)
class QueryOutcome:
    """Result of one Athena statement."""

    label: str
    state: str
    scanned_bytes: int
    millis: int
    reason: str | None

    @property
    def ok(self) -> bool:
        return self.state == "SUCCEEDED"


def _configure_logging(level: str) -> None:
    logging.basicConfig(
        level=level.upper(),
        format="%(asctime)s | %(levelname)-7s | %(name)s | %(message)s",
    )
    for noisy in ("botocore", "urllib3"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def _run(client: Any, sql: str, *, label: str, output: str, workgroup: str) -> QueryOutcome:
    """Execute one statement and block until Athena reaches a terminal state."""
    execution_id = client.start_query_execution(
        QueryString=sql,
        WorkGroup=workgroup,
        ResultConfiguration={"OutputLocation": output},
    )["QueryExecutionId"]

    deadline = time.monotonic() + POLL_TIMEOUT_SECONDS
    while True:
        execution = client.get_query_execution(QueryExecutionId=execution_id)["QueryExecution"]
        status = execution["Status"]
        if status["State"] in TERMINAL_STATES:
            break
        if time.monotonic() > deadline:
            client.stop_query_execution(QueryExecutionId=execution_id)
            return QueryOutcome(label, "TIMED_OUT", 0, 0, f"exceeded {POLL_TIMEOUT_SECONDS:.0f}s")
        time.sleep(POLL_SECONDS)

    stats = execution.get("Statistics", {})
    return QueryOutcome(
        label=label,
        state=status["State"],
        scanned_bytes=int(stats.get("DataScannedInBytes", 0)),
        millis=int(stats.get("TotalExecutionTimeInMillis", 0)),
        reason=status.get("StateChangeReason"),
    )


def _fetch_rows(client: Any, sql: str, *, output: str, workgroup: str) -> list[list[str]]:
    """Run a query and return its result rows, header included."""
    execution_id = client.start_query_execution(
        QueryString=sql,
        WorkGroup=workgroup,
        ResultConfiguration={"OutputLocation": output},
    )["QueryExecutionId"]

    deadline = time.monotonic() + POLL_TIMEOUT_SECONDS
    while True:
        execution = client.get_query_execution(QueryExecutionId=execution_id)["QueryExecution"]
        state = execution["Status"]["State"]
        if state in TERMINAL_STATES:
            break
        if time.monotonic() > deadline:
            client.stop_query_execution(QueryExecutionId=execution_id)
            raise TimeoutError(f"verification query exceeded {POLL_TIMEOUT_SECONDS:.0f}s")
        time.sleep(POLL_SECONDS)

    if state != "SUCCEEDED":
        raise RuntimeError(execution["Status"].get("StateChangeReason", state))

    result = client.get_query_results(QueryExecutionId=execution_id)
    return [
        [cell.get("VarCharValue", "") for cell in row["Data"]]
        for row in result["ResultSet"]["Rows"]
    ]


def _render(outcomes: list[QueryOutcome]) -> Table:
    table = Table(title="Athena DDL application")
    table.add_column("Statement", overflow="fold")
    table.add_column("State")
    table.add_column("Scanned", justify="right")
    table.add_column("Time", justify="right")
    for o in outcomes:
        state = f"[green]{o.state}[/green]" if o.ok else f"[red]{o.state}[/red]"
        table.add_row(o.label, state, f"{o.scanned_bytes:,} B", f"{o.millis:,} ms")
    return table


@app.command()
def main(
    ddl_dir: str = typer.Option(
        str(DEFAULT_DDL_DIR),
        help="Directory of .sql files to apply.",
    ),
    verify: bool = typer.Option(
        False,
        "--verify",
        help="After applying, query the table and print rows per snapshot date.",
    ),
    dry_run: bool = typer.Option(
        False,
        "--dry-run",
        help="List the statements that would be applied without executing them.",
    ),
    log_level: str = typer.Option("INFO", help="Logging level."),
) -> None:
    """Apply every .sql file in ``ddl_dir``, in filename order."""
    _configure_logging(log_level)
    aws = get_aws()

    if not aws.athena_output:
        console.print("[red]ATHENA_OUTPUT_S3 is not set; Athena needs a results location.[/red]")
        raise typer.Exit(code=2)

    ddl_path = Path(ddl_dir)
    files = sorted(ddl_path.glob("*.sql"))
    if not files:
        console.print(f"[yellow]No .sql files under {ddl_path}.[/yellow]")
        raise typer.Exit(code=0)

    console.print(
        Panel.fit(
            f"[bold]Glue catalog DDL[/bold]\n"
            f"source=[cyan]{ddl_path}[/cyan]  files=[cyan]{len(files)}[/cyan]  "
            f"workgroup=[cyan]{aws.athena_workgroup}[/cyan]  "
            f"results=[cyan]{aws.athena_output}[/cyan]  "
            f"dry_run=[cyan]{dry_run}[/cyan]",
            border_style="blue",
        )
    )

    if dry_run:
        for path in files:
            console.print(f"  would apply [cyan]{path.name}[/cyan]")
        raise typer.Exit(code=0)

    client = boto3.client("athena", region_name=aws.region)
    outcomes: list[QueryOutcome] = []

    for path in files:
        sql = path.read_text(encoding="utf-8").strip().rstrip(";")
        try:
            outcome = _run(
                client,
                sql,
                label=path.name,
                output=aws.athena_output,
                workgroup=aws.athena_workgroup,
            )
        except (BotoCoreError, ClientError) as exc:
            outcome = QueryOutcome(path.name, "ERROR", 0, 0, str(exc))
        outcomes.append(outcome)
        if not outcome.ok:
            console.print(f"[red]{path.name} failed:[/red] {outcome.reason}")
            break

    console.print("\n", _render(outcomes))

    failed = [o for o in outcomes if not o.ok]
    if failed:
        console.print(
            Panel.fit(
                f"[bold red]{len(failed)} statement(s) failed; catalogue not fully "
                f"applied.[/bold red]",
                border_style="red",
            )
        )
        sys.exit(1)

    if verify:
        console.print("\n[bold]Verification — rows per snapshot date[/bold]")
        try:
            rows = _fetch_rows(
                client,
                VERIFY_SQL,
                output=aws.athena_output,
                workgroup=aws.athena_workgroup,
            )
        except (BotoCoreError, ClientError, RuntimeError, TimeoutError) as exc:
            console.print(f"[red]Verification query failed:[/red] {exc}")
            sys.exit(1)

        table = Table()
        for header in rows[0]:
            table.add_column(header)
        for row in rows[1:]:
            table.add_row(*row)
        console.print(table)

    console.print(
        Panel.fit(
            f"[bold green]Applied {len(outcomes)} statement(s) to the Glue catalog.[/bold green]",
            border_style="green",
        )
    )


if __name__ == "__main__":
    app()
