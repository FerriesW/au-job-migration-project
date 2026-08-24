"""Reconcile the S3 raw landing zone against GCS, copying whatever is missing.

`ingest_adzuna.py` writes both landing zones at ingest time, but that only
covers snapshots taken after dual-write existed, and a soft-failed S3 write
leaves a gap that re-running ingest can only repair inside Adzuna's 30-day
window. Older partitions exist nowhere but GCS, so copying from GCS is the
only repair path for them — this script is that path.

Objects are compared by MD5, not by presence, so a partition that was written
to only one cloud, or written twice with different bytes, is detected and
overwritten rather than silently skipped.
"""

from __future__ import annotations

import base64
import logging
import sys
from dataclasses import dataclass
from enum import StrEnum
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
from google.cloud import storage  # type: ignore[attr-defined]  # noqa: E402

from adzuna_pipeline.config import get_aws, get_gcp  # noqa: E402

LOGGER: Final[logging.Logger] = logging.getLogger(__name__)

app = typer.Typer(add_completion=False, help="Mirror the GCS raw landing zone into S3.")
console = Console()


class Action(StrEnum):
    """What reconciliation decided to do with one object."""

    IN_SYNC = "in sync"
    MISSING = "missing in S3"
    DIVERGED = "checksum differs"


@dataclass(frozen=True)
class Comparison:
    """One GCS object weighed against its S3 counterpart."""

    key: str
    size_bytes: int
    gcs_md5: str
    s3_md5: str | None

    @property
    def action(self) -> Action:
        if self.s3_md5 is None:
            return Action.MISSING
        return Action.IN_SYNC if self.s3_md5 == self.gcs_md5 else Action.DIVERGED

    @property
    def needs_copy(self) -> bool:
        return self.action is not Action.IN_SYNC


def _configure_logging(level: str) -> None:
    logging.basicConfig(
        level=level.upper(),
        format="%(asctime)s | %(levelname)-7s | %(name)s | %(message)s",
    )
    for noisy in ("botocore", "urllib3", "google.auth"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def _gcs_md5_hex(blob: Any) -> str:
    """Return a GCS blob's MD5 as hex, matching the form S3 reports in ETag."""
    return base64.b64decode(blob.md5_hash).hex()


def _s3_md5_hex(client: Any, bucket: str, key: str) -> str | None:
    """Return the S3 object's MD5 as hex, or None when the object is absent.

    ETag equals the MD5 only for objects uploaded in a single part. Everything
    this pipeline writes is a few hundred kilobytes at most, well under the
    multipart threshold, so the comparison is sound here. A multipart ETag
    carries a ``-<parts>`` suffix; treat that as unknown rather than compare
    it to a real digest and report a false divergence.
    """
    try:
        etag = client.head_object(Bucket=bucket, Key=key)["ETag"].strip('"')
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code") in {"404", "NoSuchKey"}:
            return None
        raise
    return None if "-" in etag else etag


def _compare(gcs_bucket: Any, s3_client: Any, s3_bucket: str, prefix: str) -> list[Comparison]:
    """Weigh every GCS object under ``prefix`` against its S3 counterpart."""
    comparisons: list[Comparison] = []
    for blob in gcs_bucket.list_blobs(prefix=prefix):
        comparisons.append(
            Comparison(
                key=blob.name,
                size_bytes=blob.size,
                gcs_md5=_gcs_md5_hex(blob),
                s3_md5=_s3_md5_hex(s3_client, s3_bucket, blob.name),
            )
        )
    return sorted(comparisons, key=lambda c: c.key)


def _find_orphans(s3_client: Any, s3_bucket: str, prefix: str, gcs_keys: set[str]) -> list[str]:
    """Return S3 keys under ``prefix`` that GCS does not have.

    Reported, never deleted: the pipeline identity is deliberately denied
    ``s3:DeleteObject``, and a landing zone should not be able to erase itself.
    An orphan means either a stale test object or a GCS deletion that was not
    mirrored, and both want a human to look.
    """
    orphans: list[str] = []
    paginator = s3_client.get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=s3_bucket, Prefix=prefix):
        for obj in page.get("Contents", []):
            if obj["Key"] not in gcs_keys:
                orphans.append(str(obj["Key"]))
    return sorted(orphans)


def _copy(gcs_bucket: Any, s3_client: Any, s3_bucket: str, comparison: Comparison) -> str:
    """Copy one object GCS -> S3 and return the resulting S3 MD5.

    The bytes are passed through unchanged — the object was already written in
    its final form at ingest time, so re-encoding it here would break the
    byte-for-byte mirror the two landing zones are supposed to maintain.
    """
    data = gcs_bucket.blob(comparison.key).download_as_bytes(raw_download=True)
    s3_client.put_object(
        Bucket=s3_bucket,
        Key=comparison.key,
        Body=data,
        ContentType="application/x-ndjson",
        ContentEncoding="gzip",
        CacheControl="no-cache",
    )
    verified = _s3_md5_hex(s3_client, s3_bucket, comparison.key)
    return verified or "(unverifiable)"


def _render(comparisons: list[Comparison], results: dict[str, str]) -> Table:
    table = Table(title="GCS -> S3 landing-zone reconciliation")
    table.add_column("Object key", overflow="fold")
    table.add_column("Size", justify="right")
    table.add_column("Status")
    table.add_column("Verified MD5", overflow="fold")
    for c in comparisons:
        if c.action is Action.IN_SYNC:
            status = "[green]in sync[/green]"
            digest = c.gcs_md5
        elif c.key in results:
            ok = results[c.key] == c.gcs_md5
            status = "[green]copied[/green]" if ok else "[red]MISMATCH[/red]"
            digest = results[c.key]
        else:
            status = f"[yellow]{c.action}[/yellow]"
            digest = "—"
        table.add_row(c.key, f"{c.size_bytes:,}", status, digest)
    return table


@app.command()
def main(
    prefix: str = typer.Option(
        "",
        help="Object-key prefix to reconcile; empty means the whole bucket.",
    ),
    dry_run: bool = typer.Option(
        False,
        "--dry-run",
        help="Report what would be copied without writing anything to S3.",
    ),
    log_level: str = typer.Option("INFO", help="Logging level."),
) -> None:
    """Copy every GCS object missing from — or differing in — S3."""
    _configure_logging(log_level)
    gcp, aws = get_gcp(), get_aws()

    console.print(
        Panel.fit(
            f"[bold]Landing-zone reconciliation[/bold]\n"
            f"source=[cyan]gs://{gcp.bucket_raw}/{prefix}[/cyan]  "
            f"target=[cyan]s3://{aws.bucket_raw}/{prefix}[/cyan]  "
            f"region=[cyan]{aws.region}[/cyan]  "
            f"dry_run=[cyan]{dry_run}[/cyan]",
            border_style="blue",
        )
    )

    gcs_bucket = storage.Client(project=gcp.project_id).bucket(gcp.bucket_raw)
    s3_client = boto3.client("s3", region_name=aws.region)

    comparisons = _compare(gcs_bucket, s3_client, aws.bucket_raw, prefix)
    if not comparisons:
        console.print("[yellow]No objects found under that prefix in GCS.[/yellow]")
        raise typer.Exit(code=0)

    orphans = _find_orphans(s3_client, aws.bucket_raw, prefix, {c.key for c in comparisons})
    pending = [c for c in comparisons if c.needs_copy]
    results: dict[str, str] = {}
    failures: list[str] = []

    if not dry_run:
        for c in pending:
            try:
                results[c.key] = _copy(gcs_bucket, s3_client, aws.bucket_raw, c)
            except (BotoCoreError, ClientError) as exc:
                failures.append(c.key)
                console.print(f"[red]Copy failed for {c.key}:[/red] {exc}")

    console.print("\n", _render(comparisons, results))

    if orphans:
        console.print(
            f"\n[yellow]{len(orphans)} object(s) in S3 with no GCS counterpart "
            f"(not deleted — this identity has no s3:DeleteObject):[/yellow]"
        )
        for key in orphans:
            console.print(f"  [yellow]orphan[/yellow]  {key}")

    expected = {c.key: c.gcs_md5 for c in comparisons}
    mismatched = [key for key, digest in results.items() if digest != expected[key]]

    if dry_run:
        console.print(
            Panel.fit(
                f"[bold yellow]Dry run — {len(pending)} of {len(comparisons)} objects "
                f"would be copied.[/bold yellow]",
                border_style="yellow",
            )
        )
    elif failures or mismatched:
        console.print(
            Panel.fit(
                f"[bold red]{len(failures)} copy failure(s), "
                f"{len(mismatched)} checksum mismatch(es).[/bold red]",
                border_style="red",
            )
        )
    else:
        console.print(
            Panel.fit(
                f"[bold green]Landing zones in sync — {len(comparisons)} objects, "
                f"{len(pending)} copied, all checksums verified.[/bold green]",
                border_style="green",
            )
        )

    sys.exit(1 if failures or mismatched else 0)


if __name__ == "__main__":
    app()
