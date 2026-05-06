# dbt Project — au_jobs_radar

Three-layer warehouse model for the AU Migration & Jobs Radar.

## Layers

```
raw         → landed by Python (Adzuna API, manual seeds)
staging     → cleaned + renamed; views (cheap)
intermediate → joined / enriched (incl. LLM extracts); tables
marts       → BI-facing facts and dimensions; tables
```

## Quickstart

```bash
# from the repo root, after .env + gcp-credentials.json are in place:
cp dbt/profiles.yml.example ~/.dbt/profiles.yml
# edit ~/.dbt/profiles.yml — replace project + keyfile path

uv sync --extra dbt
uv run dbt debug --project-dir dbt
uv run dbt seed  --project-dir dbt   # once seeds land in Day 2
uv run dbt build --project-dir dbt   # full pipeline (Day 5+)
```

## Tests target (per plan §6)

| Test                          | Layer | Status   |
|-------------------------------|-------|----------|
| 4× unique + not_null on grain | marts | Day 5    |
| accepted_values (state)       | staging | Day 3  |
| relationship (anzsco code)    | marts | Day 5    |
| expression_is_true (salary)   | staging | Day 3  |
| custom: LLM coverage ≥ 90%    | intermediate | Day 4 |

`dbt build` should report **8+ tests passing** by end of Day 5.
