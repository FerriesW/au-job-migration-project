# AU Migration & Jobs Radar

> An end-to-end analytics product that quantifies the gap between Australia's
> skilled migration occupation lists (MLTSSL / STSOL / CSOL) and the real
> labour market — built for visa applicants navigating their next move.

**Status**: actively in development

---

## dbt Lineage

<p align="center">
  <img src="screenshots/dbt-lineage.png" alt="dbt model lineage from raw sources through staging and intermediate to dimensional marts" width="900"/>
</p>

The pipeline follows a staging → intermediate → marts pattern over four raw sources: Adzuna job ads, MLTSSL/STSOL/CSOL occupation lists, EOI invitations, and LLM-extracted JD signals (`staging.adzuna_jobs_llm_extract`, produced via Qwen-Turbo). Staging models normalise each source; intermediate models enrich jobs with LLM signals and map free-text job titles to ANZSCO codes via `anzsco_title_patterns`; marts surface three analytics-ready outputs — supply-demand by occupation (`fct_occupation_supply_demand_30d`), the Australian-experience barrier (`fct_local_experience_barrier`), and aggregated skills demand (`fct_skills_demand`) — alongside a dbt test enforcing ≥90% LLM extraction coverage.


## What this answers (v1)

1. **Supply vs. demand** — For each MLTSSL ANZSCO code, how many active job
   ads exist over the past 30 days, and how does that compare to EOI invitation
   counts (a proxy for competition)?
2. **Local-experience barrier** — What share of AU job descriptions explicitly
   require Australian / local experience, broken down by occupation and state?
3. **JD signals via LLM** — Skills, seniority, sponsorship willingness, and
   remote-friendliness extracted from job ad text using Qwen-Turbo.

## Tech Stack

| Layer | Tool |
|---|---|
| Ingestion | Python 3.11+ · httpx · Adzuna API |
| Cloud DW | Google BigQuery |
| Transformation | dbt Core (dbt-bigquery) |
| LLM extraction | Qwen-Turbo via Alibaba DashScope |
| BI | Power BI Desktop → Publish to Web |
| Orchestration | GitHub Actions cron (v1) |

## Project structure

```
au_job_radar_github/
├── adzuna_pipeline/        # Python ingestion package
├── dbt/                    # dbt project (staging / intermediate / marts)
├── scripts/                # one-off and CLI scripts (Day 0 verify, etc.)
├── docs/                   # setup guides, methodology notes
├── data/                   # gitignored — raw samples, exploratory outputs
├── screenshots/            # dashboard exports for README
├── .github/workflows/      # CI + scheduled runs
├── pyproject.toml          # uv-managed Python deps
├── .env.example            # env-var template
└── README.md               # you are here
```

## About me

Master of Data Science from University of Melbourne, with prior
industry-collaboration experience at SANDSTAR (Power BI dashboards adopted
by company leadership) and Metro Trains Melbourne (PostGIS spatial database).
LLM evaluation work at iFLYTEK Smart City. Open to data analyst / data
engineer roles in Melbourne.

---
