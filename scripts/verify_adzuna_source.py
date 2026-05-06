"""Verify Adzuna API data quality and volume against project thresholds."""

from __future__ import annotations

import csv
import os
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Final

import httpx
import typer
from dotenv import load_dotenv
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

PROJECT_ROOT: Final[Path] = Path(__file__).resolve().parent.parent
load_dotenv(PROJECT_ROOT / ".env")

ADZUNA_BASE: Final[str] = "https://api.adzuna.com/v1/api/jobs"
DEFAULT_PAGES: Final[int] = 5
RESULTS_PER_PAGE: Final[int] = 50
REQUEST_INTERVAL_SECONDS: Final[float] = 0.4

THRESHOLD_TOTAL_COUNT: Final[int] = 1500
THRESHOLD_SALARY_FILL: Final[float] = 0.08

app = typer.Typer(add_completion=False, help="Adzuna source verification.")
console = Console()


@dataclass
class FieldStats:
    """Per-field non-null fill rate over a sampled batch of job rows."""

    title: float = 0.0
    description: float = 0.0
    location: float = 0.0
    company: float = 0.0
    created: float = 0.0
    salary_min: float = 0.0
    salary_max: float = 0.0
    category: float = 0.0


def _credentials() -> tuple[str, str]:
    """Return Adzuna credentials from environment.

    Raises:
        typer.Exit: If either ADZUNA_APP_ID or ADZUNA_APP_KEY is missing.
    """
    app_id = os.getenv("ADZUNA_APP_ID")
    app_key = os.getenv("ADZUNA_APP_KEY")
    if not app_id or not app_key:
        console.print("[red]Missing ADZUNA_APP_ID or ADZUNA_APP_KEY in environment.[/red]")
        raise typer.Exit(code=2)
    return app_id, app_key


def fetch_page(
    client: httpx.Client,
    *,
    country: str,
    page: int,
    where: str | None,
    category: str | None,
    max_days_old: int,
    app_id: str,
    app_key: str,
    results_per_page: int = RESULTS_PER_PAGE,
) -> dict:
    """Fetch a single page from the Adzuna search endpoint.

    Args:
        client: Reusable HTTP client.
        country: Adzuna country code (e.g. ``au``).
        page: 1-indexed page number.
        where: Optional location filter; omitted when falsy.
        category: Optional category slug; omitted when falsy.
        max_days_old: Freshness window in days.
        app_id: Adzuna application id.
        app_key: Adzuna application key.
        results_per_page: Page size.

    Returns:
        Decoded JSON payload from the API.
    """
    url = f"{ADZUNA_BASE}/{country}/search/{page}"
    params: dict[str, str | int] = {
        "app_id": app_id,
        "app_key": app_key,
        "results_per_page": results_per_page,
        "max_days_old": max_days_old,
        "content-type": "application/json",
    }
    if where:
        params["where"] = where
    if category:
        params["category"] = category

    response = client.get(url, params=params, timeout=30.0)
    response.raise_for_status()
    return response.json()


def compute_field_stats(rows: list[dict]) -> FieldStats:
    """Compute per-field non-null fill rate for a list of Adzuna job records."""
    if not rows:
        return FieldStats()
    n = len(rows)

    def ratio(predicate) -> float:
        return sum(1 for r in rows if predicate(r)) / n

    return FieldStats(
        title=ratio(lambda r: bool(r.get("title"))),
        description=ratio(lambda r: bool(r.get("description"))),
        location=ratio(lambda r: bool((r.get("location") or {}).get("display_name"))),
        company=ratio(lambda r: bool((r.get("company") or {}).get("display_name"))),
        created=ratio(lambda r: bool(r.get("created"))),
        salary_min=ratio(lambda r: r.get("salary_min") is not None),
        salary_max=ratio(lambda r: r.get("salary_max") is not None),
        category=ratio(lambda r: bool((r.get("category") or {}).get("label"))),
    )


def render_field_table(stats: FieldStats, sample_size: int) -> Table:
    """Render a Rich table of field-level fill rates with target thresholds."""
    table = Table(title=f"Field completeness ({sample_size} rows)")
    table.add_column("Field", style="cyan", no_wrap=True)
    table.add_column("Fill rate", justify="right", style="magenta")
    table.add_column("Status", justify="center")

    rows: list[tuple[str, float, float]] = [
        ("title", stats.title, 1.00),
        ("description", stats.description, 0.95),
        ("location", stats.location, 0.95),
        ("company", stats.company, 0.80),
        ("created", stats.created, 1.00),
        ("category", stats.category, 0.80),
        ("salary_min", stats.salary_min, THRESHOLD_SALARY_FILL),
        ("salary_max", stats.salary_max, THRESHOLD_SALARY_FILL),
    ]
    for name, value, target in rows:
        status = "[green]OK[/green]" if value >= target else "[yellow]LOW[/yellow]"
        table.add_row(name, f"{value:.1%}", f"{status} (target ≥ {target:.0%})")
    return table


def write_sample_csv(rows: list[dict], out_path: Path) -> Path:
    """Persist sampled rows as a flat CSV.

    The Adzuna ``location.area`` array is hierarchical; element 1 is the state
    and element 2 is the city. The resulting CSV exposes country / state / city /
    suburb as separate columns for downstream analysis.
    """
    out_path.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = [
        "id", "title", "company", "location_display",
        "country", "state", "city", "suburb",
        "category", "salary_min", "salary_max", "created",
        "redirect_url", "description_snippet",
    ]
    with out_path.open("w", encoding="utf-8", newline="") as fp:
        writer = csv.DictWriter(fp, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            location = row.get("location") or {}
            areas = location.get("area") or []
            company = row.get("company") or {}
            category = row.get("category") or {}
            description = (row.get("description") or "").replace("\n", " ").strip()
            writer.writerow({
                "id": row.get("id"),
                "title": row.get("title"),
                "company": company.get("display_name"),
                "location_display": location.get("display_name"),
                "country": areas[0] if len(areas) > 0 else None,
                "state": areas[1] if len(areas) > 1 else None,
                "city": areas[2] if len(areas) > 2 else None,
                "suburb": areas[3] if len(areas) > 3 else None,
                "category": category.get("label"),
                "salary_min": row.get("salary_min"),
                "salary_max": row.get("salary_max"),
                "created": row.get("created"),
                "redirect_url": row.get("redirect_url"),
                "description_snippet": description[:240],
            })
    return out_path


def _run_multi_city(
    cities: list[str],
    *,
    app_id: str,
    app_key: str,
    country: str,
    category: str | None,
    max_days_old: int,
    pages: int,
) -> int:
    """Execute verification across multiple cities and aggregate counts.

    Returns:
        Process exit code: 0 if combined volume meets the threshold, 1 otherwise.
    """
    console.print(Panel.fit(
        f"[bold]Adzuna multi-city verification[/bold]\n"
        f"cities=[cyan]{', '.join(cities)}[/cyan]  "
        f"category=[cyan]{category or '(all)'}[/cyan]  days=[cyan]{max_days_old}[/cyan]",
        border_style="blue",
    ))

    summary = Table(title="Per-city totals")
    summary.add_column("City", style="cyan")
    summary.add_column("Total count", justify="right", style="magenta")
    summary.add_column("Sampled", justify="right")

    aggregated_rows: list[dict] = []
    grand_total = 0

    with httpx.Client() as client:
        for city in cities:
            console.print(f"\n[bold]→ {city}[/bold]")
            city_rows: list[dict] = []
            city_total: int | None = None
            for page in range(1, pages + 1):
                try:
                    payload = fetch_page(
                        client,
                        country=country,
                        page=page,
                        where=city,
                        category=category,
                        max_days_old=max_days_old,
                        app_id=app_id,
                        app_key=app_key,
                    )
                except httpx.HTTPStatusError as exc:
                    console.print(f"[red]HTTP {exc.response.status_code} on page {page}[/red]")
                    break
                if city_total is None:
                    city_total = int(payload.get("count", 0))
                city_rows.extend(payload.get("results", []))
                time.sleep(REQUEST_INTERVAL_SECONDS)
            grand_total += city_total or 0
            aggregated_rows.extend(city_rows)
            summary.add_row(city, f"{(city_total or 0):,}", f"{len(city_rows)}")
            console.print(f"  total {city_total:,}  sampled {len(city_rows)}")

    console.print("\n", summary)

    if aggregated_rows:
        stats = compute_field_stats(aggregated_rows)
        console.print(render_field_table(stats, len(aggregated_rows)))
        ts = datetime.now(tz=timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        sample_path = PROJECT_ROOT / "data" / "samples" / f"adzuna_multi_{ts}.csv"
        write_sample_csv(aggregated_rows, sample_path)
        console.print(f"\n[bold]Sample CSV:[/bold] {sample_path}")

    count_ok = grand_total >= THRESHOLD_TOTAL_COUNT
    verdict = (
        f"[bold green]GO[/bold green] — combined {grand_total:,} ≥ {THRESHOLD_TOTAL_COUNT:,}."
        if count_ok else
        f"[bold red]NO-GO[/bold red] — combined {grand_total:,} < {THRESHOLD_TOTAL_COUNT:,}."
    )
    console.print(Panel.fit(
        f"  Combined total across {len(cities)} cities: [bold]{grand_total:,}[/bold]\n\n{verdict}",
        title="Verdict", border_style="green" if count_ok else "red",
    ))
    return 0 if count_ok else 1


@app.command()
def main(
    where: str = typer.Option(
        "Melbourne",
        help="Location filter. Pass '' for nationwide. Comma-separated values "
             "trigger per-city aggregation.",
    ),
    category: str = typer.Option("it-jobs", help="Adzuna category slug; '' to disable."),
    max_days_old: int = typer.Option(30, help="Freshness window in days."),
    country: str = typer.Option("au", help="Adzuna country code."),
    pages: int = typer.Option(DEFAULT_PAGES, help="Pages to sample (50 rows per page)."),
) -> None:
    """Run Adzuna source verification and emit a Go / No-Go verdict."""
    app_id, app_key = _credentials()
    category_arg = category or None

    cities = [c.strip() for c in where.split(",") if c.strip()] if where else [None]
    if len(cities) > 1:
        sys.exit(_run_multi_city(
            cities,
            app_id=app_id,
            app_key=app_key,
            country=country,
            category=category_arg,
            max_days_old=max_days_old,
            pages=pages,
        ))

    where_one = cities[0]
    where_label = where_one or "(nationwide)"
    console.print(Panel.fit(
        f"[bold]Adzuna verification[/bold]\n"
        f"country=[cyan]{country}[/cyan]  where=[cyan]{where_label}[/cyan]  "
        f"category=[cyan]{category_arg or '(all)'}[/cyan]  "
        f"days=[cyan]{max_days_old}[/cyan]  pages=[cyan]{pages}[/cyan]",
        border_style="blue",
    ))

    rows: list[dict] = []
    total_count: int | None = None

    with httpx.Client() as client:
        for page in range(1, pages + 1):
            try:
                payload = fetch_page(
                    client,
                    country=country,
                    page=page,
                    where=where_one,
                    category=category_arg,
                    max_days_old=max_days_old,
                    app_id=app_id,
                    app_key=app_key,
                )
            except httpx.HTTPStatusError as exc:
                console.print(f"[red]HTTP {exc.response.status_code} on page {page}[/red]")
                if page == 1:
                    raise typer.Exit(code=1)
                break
            if total_count is None:
                total_count = int(payload.get("count", 0))
                console.print(f"[bold green]Total matching:[/bold green] {total_count:,}")
            rows.extend(payload.get("results", []))
            console.print(f"  page {page}: {len(payload.get('results', []))} rows  (cumulative {len(rows)})")
            time.sleep(REQUEST_INTERVAL_SECONDS)

    if not rows:
        console.print("[red]Empty result set; check filters and credentials.[/red]")
        raise typer.Exit(code=1)

    stats = compute_field_stats(rows)
    console.print(render_field_table(stats, len(rows)))

    ts = datetime.now(tz=timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    sample_path = PROJECT_ROOT / "data" / "samples" / f"adzuna_{ts}.csv"
    write_sample_csv(rows, sample_path)
    console.print(f"\n[bold]Sample CSV:[/bold] {sample_path}")

    count_ok = (total_count or 0) >= THRESHOLD_TOTAL_COUNT
    salary_ok = max(stats.salary_min, stats.salary_max) >= THRESHOLD_SALARY_FILL
    core_ok = (
        stats.title >= 0.99
        and stats.description >= 0.95
        and stats.location >= 0.95
        and stats.created >= 0.99
    )

    verdict_lines = [
        f"  Total {total_count:,} "
        f"{'[green]≥[/green]' if count_ok else '[red]<[/red]'} {THRESHOLD_TOTAL_COUNT:,}",
        f"  Core fields: {'[green]OK[/green]' if core_ok else '[yellow]partial[/yellow]'}",
        f"  Salary fill ≥ {THRESHOLD_SALARY_FILL:.0%}: "
        f"{'[green]yes[/green]' if salary_ok else '[yellow]no[/yellow]'} "
        f"(min={stats.salary_min:.1%}, max={stats.salary_max:.1%})",
    ]
    if count_ok and core_ok:
        verdict = "[bold green]GO[/bold green]"
    elif count_ok:
        verdict = "[bold yellow]NEEDS-REVIEW[/bold yellow]"
    else:
        verdict = "[bold red]NO-GO[/bold red]"

    console.print(Panel.fit(
        "\n".join(verdict_lines) + f"\n\n{verdict}",
        title="Verdict",
        border_style="green" if count_ok and core_ok else "yellow",
    ))


if __name__ == "__main__":
    app()
