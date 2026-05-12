-- =====================================================================
-- Day 5 Marts Layer Verification — au-jobs-radar
-- Run in BigQuery Console or via: bq query --use_legacy_sql=false < this.sql
-- Or via: uv run --env-file .env python -c "..."
-- =====================================================================

-- (1) Dataset & table inventory ----------------------------------------
SELECT table_name, row_count, ROUND(size_bytes/1024/1024, 2) AS size_mb
FROM `au-jobs-radar.marts.__TABLES__`
ORDER BY table_name;

-- (2) dim_occupation health -------------------------------------------
SELECT
  COUNT(*)                                AS total_anzsco_codes,
  COUNTIF(is_mltssl)                      AS mltssl_count,
  COUNTIF(is_stsol)                       AS stsol_count,
  COUNTIF(is_csol)                        AS csol_count,
  COUNTIF(occupation_name IS NULL)        AS missing_name_count
FROM `au-jobs-radar.marts.dim_occupation`;

-- (3) Q1 — Top 10 supply (should show Software Engineer #1 @ 456) -----
SELECT
  anzsco_code,
  occupation_name,
  list_membership,
  jobs_count_30d,
  eoi_invitations_recent,
  ROUND(eoi_per_job_ratio, 3) AS eoi_per_job_ratio
FROM `au-jobs-radar.marts.fct_occupation_supply_demand_30d`
WHERE jobs_count_30d > 0
ORDER BY jobs_count_30d DESC
LIMIT 10;

-- (4) Q2 — Top 10 local-experience barriers ---------------------------
SELECT
  anzsco_code,
  state,
  total_jobs,
  ROUND(local_experience_pct, 3) AS local_exp_pct,
  ROUND(sponsorship_yes_pct, 3)  AS sponsor_yes_pct
FROM `au-jobs-radar.marts.fct_local_experience_barrier`
ORDER BY total_jobs DESC
LIMIT 10;

-- (5) Q3 — Top skill per state (should show AWS dominant) -------------
SELECT state, skill, mention_count
FROM `au-jobs-radar.marts.fct_skills_demand`
QUALIFY ROW_NUMBER() OVER (PARTITION BY state ORDER BY mention_count DESC) <= 3
ORDER BY state, mention_count DESC;
