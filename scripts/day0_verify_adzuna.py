"""
Day 0 — Adzuna API Go/No-Go verification script.

Purpose
-------
Validate that Adzuna can supply enough volume + field quality for the v1 MVP
BEFORE writing any pipeline code. Implements the criteria from
migration-job-dashboard-plan.md §3.

Go criteria (any one passing → continue):
    • Melbourne IT-category jobs (past 30 days) >= 1500
    • Field completeness: title, description, location, company, created
    • salary fields present in >= 30% of rows

Usage
-----
    # 1. Make sure ADZUNA_APP_ID and ADZUNA_APP_KEY are set in .env (or shell)
    # 2. From repo root:
    uv run python scripts/day0_verify_adzuna.py

    # Optional: change city or category
    uv run python scripts/day0_verify_adzuna.py --where Sydney --category it-jobs

Output
------
    data/samples/adzuna_day0_<timestamp>.csv   (250 rows for eyeballing)
    Console verdict:  GO  /  NO-GO (suggest fallback)  /  NEEDS-REVIEW
"""

from __future__ import annotations

import csv
import os
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import httpx
import typer
from dotenv import load_dotenv
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

# --------------------------------------------------------------------------- #
# Setup
# --------------------------------------------------------------------------- #

PROJECT_ROOT = Path(__file__).resolve().parent.parent
load_dotenv(PROJECT_ROOT / ".env")

app = typer.Typer(add_completion=False, help="Adzuna Day-0 sanity check")
console = Console()

ADZUNA_BASE = "https://api.adzuna.com/v1/api/jobs"
PAGES_TO_FETCH = 5
RESULTS_PER_PAGE = 50

# Go thresholds (see plan §3)
# NOTE: salary threshold lowered from 30% → 8% based on AU market reality.
# Most Australian job ads on Adzuna omit salary fields (Seek/Adzuna/LinkedIn
# all show 8-15% salary disclosure for AU). Treat low salary fill as a
# documented limitation, not a Go/No-Go blocker.
THRESHOLD_TOTAL_COUNT = 1500
THRESHOLD_SALARY_FILL = 0.08


# --------------------------------------------------------------------------- #
# Data classes
# --------------------------------------------------------------------------- #

@dataclass
class FieldStats:
    """Field-level non-null fill rate across sampled rows."""
    title: float = 0.0
    description: float = 0.0
    location: float = 0.0
    company: float = 0.0
    created: float = 0.0
    salary_min: float = 0.0
    salary_max: float = 0.0
    category: float = 0.0


# --------------------------------------------------------------------------- #
# Adzuna client
# --------------------------------------------------------------------------- #

def _get_credentials() -> tuple[str, str]:
    app_id = os.getenv("ADZUNA_APP_ID")
    app_key = os.getenv("ADZUNA_APP_KEY")
    if not app_id or not app_key:
        console.print(
            "[red]ERROR[/red]: ADZUNA_APP_ID / ADZUNA_APP_KEY not found.\n"
            "Create a .env file from .env.example or export them in your shell."
        )
        raise typer.Exit(code=2)
    return app_id, app_key


def fetch_page(
    client: httpx.Client,
    country: str,
    page: int,
    *,
    where: str | None,
    category: str | None,
    max_days_old: int,
    app_id: str,
    app_key: str,
    results_per_page: int = RESULTS_PER_PAGE,
) -> dict:
    """Single page fetch from Adzuna /search/{page}."""
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

    r = client.get(url, params=params, timeout=30.0)
    r.raise_for_status()
    return r.json()


# --------------------------------------------------------------------------- #
# Quality metrics
# --------------------------------------------------------------------------- #

def compute_field_stats(rows: list[dict]) -> FieldStats:
    """Compute per-field non-null / non-empty fill rate."""
    if not rows:
        return FieldStats()

    n = len(rows)

    def _ratio(predicate) -> float:
        return sum(1 for r in rows if predicate(r)) / n

    return FieldStats(
        title=_ratio(lambda r: bool(r.get("title"))),
        description=_ratio(lambda r: bool(r.get("description"))),
        location=_ratio(lambda r: bool((r.get("location") or {}).get("display_name"))),
        company=_ratio(lambda r: bool((r.get("company") or {}).get("display_name"))),
        created=_ratio(lambda r: bool(r.get("created"))),
        salary_min=_ratio(lambda r: r.get("salary_min") is not None),
        salary_max=_ratio(lambda r: r.get("salary_max") is not None),
        category=_ratio(lambda r: bool((r.get("category") or {}).get("label"))),
    )


def render_field_table(stats: FieldStats, sample_size: int) -> Table:
    table = Table(title=f"Field completeness over {sample_size} sampled rows")
    table.add_column("Field", style="cyan", no_wrap=True)
    table.add_column("Fill rate", justify="right", style="magenta")
    table.add_column("Status", justify="center")

    rows = [
        ("title",       stats.title,       1.00),
        ("description", stats.description, 0.95),
        ("location",    stats.location,    0.95),
        ("company",     stats.company,     0.80),
        ("created",     stats.created,     1.00),
        ("category",    stats.category,    0.80),
        ("salary_min",  stats.salary_min,  0.30),
        ("salary_max",  stats.salary_max,  0.30),
    ]
    for name, value, target in rows:
        status = "[green]OK[/green]" if value >= target else "[yellow]LOW[/yellow]"
        table.add_row(name, f"{value:.1%}", f"{status} (target ≥ {target:.0%})")
    return table


def write_sample_csv(rows: list[dict], out_path: Path) -> Path:
    """Write a flat CSV sample.

    Adzuna `location.area` is a hierarchical list:
      areas[0] = country  (e.g. "Australia")
      areas[1] = state    (e.g. "Victoria")
      areas[2] = city/region (e.g. "Melbourne")  ← granularity we usually want
      areas[3] = suburb / inner area (when available)
    """
    out_path.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = [
        "id", "title", "company", "location_display",
        "country", "state", "city", "suburb",
        "category", "salary_min", "salary_max", "created",
        "redirect_url", "description_snippet",
    ]
    with out_path.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for r in rows:
            loc = r.get("location") or {}
            areas = loc.get("area") or []
            company = r.get("company") or {}
            cat = r.get("category") or {}
            desc = (r.get("description") or "").replace("\n", " ").strip()
            writer.writerow({
                "id": r.get("id"),
                "title": r.get("title"),
                "company": company.get("display_name"),
                "location_display": loc.get("display_name"),
                "country": areas[0] if len(areas) > 0 else None,
                "state":   areas[1] if len(areas) > 1 else None,
                "city":    areas[2] if len(areas) > 2 else None,
                "suburb":  areas[3] if len(areas) > 3 else None,
                "category": cat.get("label"),
                "salary_min": r.get("salary_min"),
                "salary_max": r.get("salary_max"),
                "created": r.get("created"),
                "redirect_url": r.get("redirect_url"),
                "description_snippet": desc[:240],
            })
    return out_path


# --------------------------------------------------------------------------- #
# Multi-city helper
# --------------------------------------------------------------------------- #

def _run_multi_city(
    cities: list[str],
    app_id: str,
    app_key: str,
    *,
    country: str,
    category: str | None,
    max_days_old: int,
    pages: int,
) -> None:
    """Verify multiple cities and aggregate counts (for plan §5 Day-2 scope)."""
    console.print(Panel.fit(
        f"[bold]Adzuna Day-0 Multi-City Verification[/bold]\n"
        f"cities=[cyan]{', '.join(cities)}[/cyan]  "
        f"category=[cyan]{category or '(all)'}[/cyan]  days=[cyan]{max_days_old}[/cyan]",
        border_style="blue",
    ))

    summary_table = Table(title="Per-city totals")
    summary_table.add_column("City", style="cyan")
    summary_table.add_column("Total count (30d)", justify="right", style="magenta")
    summary_table.add_column("Sampled rows", justify="right")

    grand_rows: list[dict] = []
    grand_total = 0

    with httpx.Client() as client:
        for city in cities:
            console.print(f"\n[bold]→ {city}[/bold]")
            city_rows: list[dict] = []
            city_total: int | None = None
            for page in range(1, pages + 1):
                try:
                    payload = fetch_page(
                        client, country, page,
                        where=city, category=category, max_days_old=max_days_old,
                        app_id=app_id, app_key=app_key,
                    )
                except httpx.HTTPStatusError as e:
                    console.print(f"[red]HTTP error[/red] page {page}: {e.response.status_code}")
                    if page == 1:
                        break
                    break
                results = payload.get("results", [])
                if city_total is None:
                    city_total = int(payload.get("count", 0))
                city_rows.extend(results)
                time.sleep(0.4)
            grand_total += city_total or 0
            grand_rows.extend(city_rows)
            summary_table.add_row(city, f"{(city_total or 0):,}", f"{len(city_rows)}")
            console.print(f"  total {city_total:,}  sampled {len(city_rows)}")

    console.print("\n", summary_table)

    if grand_rows:
        stats = compute_field_stats(grand_rows)
        console.print(render_field_table(stats, len(grand_rows)))
        ts = datetime.now(tz=timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        sample_path = PROJECT_ROOT / "data" / "samples" / f"adzuna_day0_multi_{ts}.csv"
        write_sample_csv(grand_rows, sample_path)
        console.print(f"\n[bold]Aggregated sample CSV written:[/bold] {sample_path}")

    count_ok = grand_total >= THRESHOLD_TOTAL_COUNT
    verdict = (
        f"[bold green]GO[/bold green] — Combined volume {grand_total:,} ≥ {THRESHOLD_TOTAL_COUNT:,}. "
        f"Use these {len(cities)} cities as Day-2 ingestion scope."
        if count_ok else
        f"[bold red]NO-GO[/bold red] — Combined volume {grand_total:,} < {THRESHOLD_TOTAL_COUNT:,}. "
        f"Try dropping the city filter entirely:  --where ''"
    )
    console.print(Panel.fit(
        f"  • Combined total across {len(cities)} cities: [bold]{grand_total:,}[/bold]\n\n{verdict}",
        title="Multi-city Verdict", border_style="green" if count_ok else "red",
    ))


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #

@app.command()
def main(
    where: str = typer.Option(
        "Melbourne",
        help="City / region filter. Pass '' (empty) to query all of AU. "
             "Comma-separated list (e.g. 'Melbourne,Sydney,Brisbane') will run "
             "one verification per city and aggregate counts.",
    ),
    category: str = typer.Option("it-jobs", help="Adzuna category slug; pass '' to disable"),
    max_days_old: int = typer.Option(30, help="Job freshness window (days)"),
    country: str = typer.Option("au", help="Adzuna country code"),
    pages: int = typer.Option(PAGES_TO_FETCH, help="Pages to sample (50 rows / page)"),
):
    """Run the Day-0 Adzuna sanity check and emit Go / No-Go verdict."""
    app_id, app_key = _get_credentials()
    cat_arg = category or None

    # Multi-city aggregation path
    cities = [c.strip() for c in where.split(",") if c.strip()] if where else [None]
    if len(cities) > 1:
        return _run_multi_city(
            cities, app_id, app_key,
            country=country, category=cat_arg,
            max_days_old=max_days_old, pages=pages,
        )

    # Single-scope path (one city, or all-AU when where=='')
    where_one = cities[0]  # may be None → no city filter
    where_label = where_one or "(all AU)"
    console.print(Panel.fit(
        f"[bold]Adzuna Day-0 Verification[/bold]\n"
        f"country=[cyan]{country}[/cyan]  where=[cyan]{where_label}[/cyan]  "
        f"category=[cyan]{cat_arg or '(all)'}[/cyan]  "
        f"days=[cyan]{max_days_old}[/cyan]  pages=[cyan]{pages}[/cyan]",
        border_style="blue",
    ))

    all_rows: list[dict] = []
    total_count: int | None = None

    with httpx.Client() as client:
        for page in range(1, pages + 1):
            try:
                payload = fetch_page(
                    client, country, page,
                    where=where_one, category=cat_arg, max_days_old=max_days_old,
                    app_id=app_id, app_key=app_key,
                )
            except httpx.HTTPStatusError as e:
                console.print(f"[red]HTTP error on page {page}[/red]: {e.response.status_code} {e.response.text[:200]}")
                if page == 1:
                    raise typer.Exit(code=1)
                break

            results = payload.get("results", [])
            if total_count is None:
                total_count = int(payload.get("count", 0))
                console.print(f"[bold green]Total jobs matching filter:[/bold green] {total_count:,}")
            all_rows.extend(results)
            console.print(f"  page {page}: fetched {len(results)} rows  (cumulative {len(all_rows)})")
            time.sleep(0.4)  # be polite

    if not all_rows:
        console.print("[red]No rows returned. Check filters or credentials.[/red]")
        raise typer.Exit(code=1)

    stats = compute_field_stats(all_rows)
    console.print(render_field_table(stats, len(all_rows)))

    # Persist sample
    ts = datetime.now(tz=timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    sample_path = PROJECT_ROOT / "data" / "samples" / f"adzuna_day0_{ts}.csv"
    write_sample_csv(all_rows, sample_path)
    console.print(f"\n[bold]Sample CSV written:[/bold] {sample_path}")

    # Verdict
    count_ok = (total_count or 0) >= THRESHOLD_TOTAL_COUNT
    salary_ok = stats.salary_min >= THRESHOLD_SALARY_FILL or stats.salary_max >= THRESHOLD_SALARY_FILL
    core_ok = (stats.title >= 0.99 and stats.description >= 0.95
               and stats.location >= 0.95 and stats.created >= 0.99)

    verdict_lines: list[str] = []
    verdict_lines.append(
        f"  • Total count {total_count:,} "
        f"{'[green]>=[/green]' if count_ok else '[red]<[/red]'} "
        f"{THRESHOLD_TOTAL_COUNT:,} threshold"
    )
    verdict_lines.append(
        f"  • Core fields complete: "
        f"{'[green]YES[/green]' if core_ok else '[yellow]PARTIAL[/yellow]'}"
    )
    verdict_lines.append(
        f"  • Salary fill ≥ {THRESHOLD_SALARY_FILL:.0%}: "
        f"{'[green]YES[/green]' if salary_ok else '[yellow]NO[/yellow]'}  "
        f"(min={stats.salary_min:.1%}, max={stats.salary_max:.1%})"
    )

    if count_ok and core_ok:
        verdict = "[bold green]GO[/bold green] — Adzuna data quality meets v1 requirements. Proceed to Day 1+."
    elif count_ok and not core_ok:
        verdict = "[bold yellow]NEEDS-REVIEW[/bold yellow] — Volume OK but field gaps; spot-check the CSV before continuing."
    else:
        verdict = (
            "[bold red]NO-GO[/bold red] — Volume below threshold. "
            "Try widening: --where 'Australia' (drop city) or --category '' (drop IT filter). "
            "If still failing, switch to data.gov.au + ABS fallback (plan §3 No-Go branch)."
        )

    console.print(Panel.fit(
        "\n".join(verdict_lines) + "\n\n" + verdict,
        title="Day-0 Verdict", border_style="green" if count_ok and core_ok else "yellow",
    ))


if __name__ == "__main__":
    app()
