"""GCP connectivity smoke test for the BigQuery + Cloud Storage stack."""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Final

from dotenv import load_dotenv
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

PROJECT_ROOT: Final[Path] = Path(__file__).resolve().parent.parent
load_dotenv(PROJECT_ROOT / ".env")

REQUIRED_ENV_VARS: Final[tuple[str, ...]] = (
    "GCP_PROJECT_ID",
    "GOOGLE_APPLICATION_CREDENTIALS",
    "GCS_BUCKET_RAW",
)
EXPECTED_DATASETS: Final[frozenset[str]] = frozenset({"raw", "staging", "marts"})
SMOKE_BLOB_KEY: Final[str] = "_smoke_test/connectivity.txt"

console = Console()


def _resolve_credentials_path(value: str) -> Path:
    """Resolve a credentials path against the project root when relative."""
    path = Path(value)
    return path if path.is_absolute() else (PROJECT_ROOT / path).resolve()


def _check_environment() -> tuple[dict[str, str], list[str]]:
    """Probe required environment variables and return values plus missing keys."""
    table = Table(title="Environment variables")
    table.add_column("Key", style="cyan")
    table.add_column("Value", overflow="fold")
    table.add_column("Status", justify="center")

    values: dict[str, str] = {}
    missing: list[str] = []
    for key in REQUIRED_ENV_VARS:
        value = os.getenv(key)
        if value:
            values[key] = value
            table.add_row(key, value, "[green]OK[/green]")
        else:
            missing.append(key)
            table.add_row(key, "(unset)", "[red]MISSING[/red]")
    console.print(table)
    return values, missing


def _check_bigquery(project_id: str) -> bool:
    """Validate BigQuery dataset visibility and a tiny query round-trip."""
    from google.cloud import bigquery

    try:
        client = bigquery.Client(project=project_id)
        datasets = [d.dataset_id for d in client.list_datasets()]
        console.print(f"\n[bold]BigQuery datasets in {project_id}:[/bold] {datasets or '(none)'}")
        missing = EXPECTED_DATASETS - set(datasets)
        if missing:
            console.print(f"[yellow]Missing expected datasets:[/yellow] {sorted(missing)}")
        else:
            console.print("[green]All expected datasets present.[/green]")

        result = list(client.query("SELECT CURRENT_TIMESTAMP() AS now").result())
        console.print(f"[green]Query round-trip OK:[/green] now = {result[0].now}")
        return True
    except Exception as exc:
        console.print(f"[red]BigQuery FAILED:[/red] {exc}")
        return False


def _check_gcs(project_id: str, bucket_name: str) -> bool:
    """Validate object-level GCS access via a write/read/delete round-trip."""
    from google.cloud import storage

    try:
        client = storage.Client(project=project_id)
        bucket = client.bucket(bucket_name)
        blob = bucket.blob(SMOKE_BLOB_KEY)
        payload = f"smoke test for {project_id}"
        blob.upload_from_string(payload, content_type="text/plain")
        if blob.download_as_text() != payload:
            raise RuntimeError("GCS round-trip payload mismatch.")
        blob.delete()
        console.print(
            f"[green]GCS round-trip OK:[/green] gs://{bucket_name}/{SMOKE_BLOB_KEY}"
        )
        return True
    except Exception as exc:
        console.print(f"[red]GCS FAILED:[/red] {exc}")
        console.print(
            "[yellow]Hint:[/yellow] service account requires Storage Object Admin "
            "on the bucket; bucket-metadata permissions are not used."
        )
        return False


def main() -> int:
    """Run the full smoke-test suite and return a process exit code."""
    console.print(Panel.fit("[bold]GCP smoke test[/bold]", border_style="blue"))

    env, missing = _check_environment()
    if missing:
        console.print(f"\n[red]Set the missing variables in .env: {', '.join(missing)}[/red]")
        return 2

    cred_path = _resolve_credentials_path(env["GOOGLE_APPLICATION_CREDENTIALS"])
    if not cred_path.exists():
        console.print(f"[red]Credentials file not found:[/red] {cred_path}")
        return 2
    os.environ["GOOGLE_APPLICATION_CREDENTIALS"] = str(cred_path)
    console.print(f"\n[green]Credentials JSON:[/green] {cred_path}")

    if not _check_bigquery(env["GCP_PROJECT_ID"]):
        return 1
    if not _check_gcs(env["GCP_PROJECT_ID"], env["GCS_BUCKET_RAW"]):
        return 1

    console.print(Panel.fit(
        "[bold green]All GCP smoke tests passed.[/bold green]",
        border_style="green",
    ))
    return 0


if __name__ == "__main__":
    sys.exit(main())
