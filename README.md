# AU Migration & Jobs Radar

> An end-to-end analytics product that quantifies the gap between Australia's
> skilled migration occupation lists (MLTSSL / STSOL / CSOL) and the real
> labour market — built for visa applicants navigating their next move.

**Status**: Day 1 — project skeleton initialised. Live dashboard pending Day 8.

---

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

## Getting started (development)

```bash
# 1. Install uv (if not already)
# Windows: powershell -c "irm https://astral.sh/uv/install.ps1 | iex"
# macOS / Linux: curl -LsSf https://astral.sh/uv/install.sh | sh

# 2. Clone & sync dependencies
git clone <this-repo>
cd au_job_radar_github
uv sync

# 3. Configure environment
cp .env.example .env
# Edit .env with your Adzuna / GCP / DashScope credentials

# 4. Run Day-0 Adzuna sanity check
uv run python scripts/day0_verify_adzuna.py
```

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

*This is a 14-day MVP portfolio project. See `migration-job-dashboard-plan.md`
in the parent directory for the full execution plan.*
