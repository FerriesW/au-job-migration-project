"""LLM-as-judge evaluation of extraction quality with per-field accuracy report."""

from __future__ import annotations

import asyncio
import logging
import sys
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Final

import typer
from dotenv import load_dotenv
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

PROJECT_ROOT: Final[Path] = Path(__file__).resolve().parent.parent
load_dotenv(PROJECT_ROOT / ".env")

from adzuna_pipeline.config import get_gcp  # noqa: E402
from adzuna_pipeline.extraction.judge import (  # noqa: E402
    JUDGED_FIELDS,
    ExtractionJudgment,
    QwenJudge,
    Verdict,
)
from adzuna_pipeline.extraction.schema import (  # noqa: E402
    ExtractionResult,
    RemoteFriendly,
    SponsorshipSignal,
)

app = typer.Typer(add_completion=False, help="LLM-as-judge evaluation.")
console = Console()


@dataclass
class JudgedRow:
    """One row of evaluation input plus the judge's verdict."""

    job_id: str
    job_title: str
    description_text: str
    extraction: ExtractionResult
    judgment: ExtractionJudgment | None
    error: str | None


def _configure_logging(level: str) -> None:
    logging.basicConfig(
        level=level.upper(),
        format="%(asctime)s | %(levelname)-7s | %(name)s | %(message)s",
    )
    for noisy in ("httpx", "httpcore", "google.auth", "urllib3"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def _fetch_evaluation_set(sample_size: int, only_with_signal: bool) -> list[dict]:
    """Pull description + extraction pairs from BigQuery for evaluation.

    Args:
        sample_size: Maximum rows to return.
        only_with_signal: If True, restrict to descriptions that mention any
            extraction-relevant keyword. Increases the share of "interesting"
            (non-trivially-empty) rows in the evaluation.
    """
    from google.cloud import bigquery

    gcp = get_gcp()
    project = gcp.project_id
    dataset = gcp.dataset_staging

    keyword_filter = (
        "AND REGEXP_CONTAINS("
        "  LOWER(j.description_text), "
        r"  r'years|experience|sponsor|visa|remote|hybrid|wfh|python|sql|"
        r"aws|gcp|azure|java|react|kubernetes|docker'"
        ")"
        if only_with_signal else ""
    )
    query = f"""
        SELECT
          j.job_id,
          j.job_title,
          j.description_text,
          e.required_skills,
          e.years_experience,
          e.sponsorship_signal,
          e.local_experience_required,
          e.remote_friendly
        FROM `{project}.{dataset}.adzuna_jobs_llm_extract` e
        JOIN `{project}.{dataset}.stg_adzuna__jobs` j USING (job_id, snapshot_date)
        WHERE e.extraction_status = 'ok'
          AND j.description_text IS NOT NULL
          {keyword_filter}
        ORDER BY ARRAY_LENGTH(e.required_skills) DESC, LENGTH(j.description_text) DESC
        LIMIT @sample_size
    """
    client = bigquery.Client(project=project, location=gcp.location)
    job_config = bigquery.QueryJobConfig(query_parameters=[
        bigquery.ScalarQueryParameter("sample_size", "INT64", sample_size),
    ])
    rows = client.query(query, job_config=job_config).result()
    return [dict(r) for r in rows]


async def _judge_rows(
    raw_rows: list[dict],
    *,
    concurrency: int,
) -> list[JudgedRow]:
    sem = asyncio.Semaphore(concurrency)
    async with QwenJudge() as judge:
        tasks = [_judge_one(r, judge, sem) for r in raw_rows]
        return await asyncio.gather(*tasks)


async def _judge_one(
    row: dict,
    judge: QwenJudge,
    sem: asyncio.Semaphore,
) -> JudgedRow:
    extraction = ExtractionResult(
        required_skills=list(row["required_skills"] or []),
        years_experience=row["years_experience"],
        sponsorship_signal=SponsorshipSignal(row["sponsorship_signal"]),
        local_experience_required=row["local_experience_required"],
        remote_friendly=RemoteFriendly(row["remote_friendly"]),
    )
    async with sem:
        try:
            judgment = await judge.judge(row["description_text"], extraction)
            return JudgedRow(
                job_id=row["job_id"],
                job_title=row["job_title"],
                description_text=row["description_text"],
                extraction=extraction,
                judgment=judgment,
                error=None,
            )
        except Exception as exc:  # noqa: BLE001
            return JudgedRow(
                job_id=row["job_id"],
                job_title=row["job_title"],
                description_text=row["description_text"],
                extraction=extraction,
                judgment=None,
                error=str(exc)[:300],
            )


def _aggregate(judged: list[JudgedRow]) -> dict[str, Counter]:
    """Tally verdicts per field across the evaluation set."""
    tallies: dict[str, Counter] = {field: Counter() for field in JUDGED_FIELDS}
    for row in judged:
        if row.judgment is None:
            continue
        for field, verdict in row.judgment.per_field().items():
            tallies[field][verdict.value] += 1
    return tallies


def _render_summary(tallies: dict[str, Counter], judged: list[JudgedRow]) -> Table:
    table = Table(title="Per-field judgment summary")
    table.add_column("Field", style="cyan")
    table.add_column("Correct", justify="right", style="green")
    table.add_column("Incorrect", justify="right", style="red")
    table.add_column("Uncertain", justify="right", style="yellow")
    table.add_column("Accuracy", justify="right", style="magenta")
    for field in JUDGED_FIELDS:
        c = tallies[field]
        correct = c.get(Verdict.CORRECT.value, 0)
        incorrect = c.get(Verdict.INCORRECT.value, 0)
        uncertain = c.get(Verdict.UNCERTAIN.value, 0)
        denominator = correct + incorrect
        accuracy = correct / denominator if denominator else 0.0
        table.add_row(
            field,
            str(correct),
            str(incorrect),
            str(uncertain),
            f"{accuracy:.1%}" if denominator else "n/a",
        )

    n_judged = sum(1 for r in judged if r.judgment is not None)
    n_failed = sum(1 for r in judged if r.judgment is None)
    overall_correct = sum(t.get(Verdict.CORRECT.value, 0) for t in tallies.values())
    overall_total = sum(
        t.get(Verdict.CORRECT.value, 0) + t.get(Verdict.INCORRECT.value, 0)
        for t in tallies.values()
    )
    overall_accuracy = overall_correct / overall_total if overall_total else 0.0

    table.caption = (
        f"rows judged: {n_judged}  failed: {n_failed}  "
        f"overall accuracy: {overall_accuracy:.1%} "
        f"({overall_correct}/{overall_total} field judgments)"
    )
    return table


def _print_failure_examples(judged: list[JudgedRow], limit: int = 5) -> None:
    """Print up to ``limit`` rows where any field was judged incorrect."""
    failures: list[tuple[JudgedRow, list[tuple[str, str]]]] = []
    for row in judged:
        if row.judgment is None:
            continue
        bad_fields: list[tuple[str, str]] = []
        for field, fj_verdict in row.judgment.per_field().items():
            if fj_verdict == Verdict.INCORRECT:
                fj = getattr(row.judgment, field)
                bad_fields.append((field, fj.reasoning))
        if bad_fields:
            failures.append((row, bad_fields))
        if len(failures) >= limit:
            break

    if not failures:
        console.print("\n[green]No incorrect verdicts in the evaluation set.[/green]")
        return

    for row, bad_fields in failures:
        console.print(Panel.fit(
            f"[bold]{row.job_title}[/bold]  ({row.job_id})\n"
            f"[dim]{row.description_text[:240]}...[/dim]\n\n"
            + "\n".join(
                f"  [red]{field}[/red]  -> {row.extraction.model_dump().get(field)!r}\n"
                f"      reason: {reason}"
                for field, reason in bad_fields
            ),
            border_style="red",
            title="incorrect",
        ))


@app.command()
def main(
    sample_size: int = typer.Option(50, help="Rows to evaluate."),
    concurrency: int = typer.Option(5, help="Concurrent judge calls."),
    only_with_signal: bool = typer.Option(
        True,
        help="Restrict to descriptions containing extraction-relevant keywords.",
    ),
    show_failures: int = typer.Option(5, help="Number of failure cases to print."),
    log_level: str = typer.Option("INFO"),
) -> None:
    """Evaluate extraction quality via an LLM judge and print a per-field report."""
    _configure_logging(log_level)
    console.print(Panel.fit(
        f"[bold]LLM-as-judge evaluation[/bold]\n"
        f"sample_size=[cyan]{sample_size}[/cyan]  "
        f"concurrency=[cyan]{concurrency}[/cyan]  "
        f"only_with_signal=[cyan]{only_with_signal}[/cyan]",
        border_style="blue",
    ))

    raw_rows = _fetch_evaluation_set(sample_size, only_with_signal)
    if not raw_rows:
        console.print("[red]No rows to evaluate.[/red]")
        raise typer.Exit(code=1)
    console.print(f"[bold]Evaluation rows fetched:[/bold] {len(raw_rows)}")

    judged = asyncio.run(_judge_rows(raw_rows, concurrency=concurrency))

    tallies = _aggregate(judged)
    console.print("\n", _render_summary(tallies, judged))

    if show_failures > 0:
        _print_failure_examples(judged, limit=show_failures)

    overall_correct = sum(t.get(Verdict.CORRECT.value, 0) for t in tallies.values())
    overall_total = sum(
        t.get(Verdict.CORRECT.value, 0) + t.get(Verdict.INCORRECT.value, 0)
        for t in tallies.values()
    )
    accuracy = overall_correct / overall_total if overall_total else 0.0
    sys.exit(0 if accuracy >= 0.80 else 1)


if __name__ == "__main__":
    app()
