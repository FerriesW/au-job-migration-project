"""Day 5 marts verification: sanity queries against the BigQuery marts layer.

Usage (PowerShell, from project root):
    uv run --env-file .env python scripts/verify/day5_marts_check.py
"""
from __future__ import annotations

import os
from google.cloud import bigquery

PROJECT = os.environ.get("GCP_PROJECT_ID", "au-jobs-radar")
MARTS = f"{PROJECT}.{os.environ.get('BQ_DATASET_MARTS', 'marts')}"

QUERIES: list[tuple[str, str]] = [
    (
        "(1) Marts dataset inventory",
        f"""
        SELECT table_id AS table_name,
               row_count,
               ROUND(size_bytes / 1024 / 1024, 2) AS size_mb
        FROM `{MARTS}.__TABLES__`
        ORDER BY table_id
        """,
    ),
    (
        "(2) dim_occupation health",
        f"""
        SELECT
            COUNT(*)                         AS total_anzsco_codes,
            COUNTIF(is_mltssl)               AS mltssl_count,
            COUNTIF(is_stsol)                AS stsol_count,
            COUNTIF(is_csol)                 AS csol_count,
            COUNTIF(occupation_name IS NULL) AS missing_name_count
        FROM `{MARTS}.dim_occupation`
        """,
    ),
    (
        "(3) Q1 top-10 supply with ceiling + grants demand proxy",
        f"""
        SELECT
            anzsco_code,
            occupation_name,
            anzsco_unit_group,
            list_membership,
            jobs_count_30d,
            annual_ceiling,
            grants_py24_25,
            ROUND(jobs_to_ceiling_ratio, 3) AS jobs_to_ceiling_ratio,
            ROUND(grants_to_jobs_ratio, 3)  AS grants_to_jobs_ratio
        FROM `{MARTS}.fct_occupation_supply_demand_30d`
        WHERE jobs_count_30d > 0
        ORDER BY jobs_count_30d DESC
        LIMIT 10
        """,
    ),
    (
        "(4) Q2 top-10 local-experience barriers",
        f"""
        SELECT
            anzsco_code,
            state,
            total_jobs,
            ROUND(local_experience_pct, 3) AS local_exp_pct,
            ROUND(sponsorship_yes_pct, 3)  AS sponsor_yes_pct
        FROM `{MARTS}.fct_local_experience_barrier`
        ORDER BY total_jobs DESC
        LIMIT 10
        """,
    ),
    (
        "(5) Q3 top-3 skills per state",
        f"""
        SELECT state, skill, mention_count
        FROM `{MARTS}.fct_skills_demand`
        QUALIFY ROW_NUMBER() OVER (PARTITION BY state ORDER BY mention_count DESC) <= 3
        ORDER BY state, mention_count DESC
        """,
    ),
]


def print_rows(rows: list[bigquery.Row]) -> None:
    if not rows:
        print("  (no rows)")
        return
    cols = list(rows[0].keys())
    widths = {c: max(len(c), *(len(str(r[c])) for r in rows)) for c in cols}
    header = " | ".join(c.ljust(widths[c]) for c in cols)
    print("  " + header)
    print("  " + "-+-".join("-" * widths[c] for c in cols))
    for r in rows:
        print("  " + " | ".join(str(r[c]).ljust(widths[c]) for c in cols))


def main() -> None:
    client = bigquery.Client(project=PROJECT)
    print(f"Project: {client.project}\nDataset: {MARTS}\n")
    for title, sql in QUERIES:
        print("=" * 78)
        print(title)
        print("=" * 78)
        try:
            rows = list(client.query(sql).result())
            print_rows(rows)
        except Exception as exc:  # noqa: BLE001
            print(f"  ERROR: {exc}")
        print()


if __name__ == "__main__":
    main()
