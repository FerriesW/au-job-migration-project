---
status: accepted
---

# CI/CD on GitHub Actions: hermetic PR gate, keyless scheduled CD

## Decision

The project runs two GitHub Actions workflows with a hard boundary between them.
**CI** (`ci.yml`) is a hermetic PR gate that runs only credential-free checks —
`ruff`, `mypy --strict`, `pytest`, and offline `dbt parse` — and holds **zero
secrets**. **CD** (`cd.yml`) is a monthly (and manually dispatchable) `dbt build`
against live BigQuery that authenticates **keylessly via OIDC / Workload Identity
Federation**; no long-lived service-account JSON key is stored in GitHub. CD's
first increment is `dbt build` only (rebuild staging/intermediate/marts from data
already in `raw`); ingestion and LLM extraction — which require the Adzuna and
DashScope API keys — are deferred to a later increment to keep the initial secret
surface at zero stored keys.

## Why

- **A green check must mean the code is correct.** If the PR gate ran `dbt build`
  against live BigQuery, a build could go red because the Adzuna data drifted or
  a quota ran out, with no code change — training us to ignore red. Keeping the
  gate hermetic makes its signal trustworthy, fast, and free.
- **Smallest blast radius for secrets.** The PR-triggered workflow is the most
  broadly and frequently triggered surface in the repo (and the repo is public).
  Putting GCP/Snowflake/DashScope credentials there is the worst place for them.
  Secrets live only in the low-frequency, tightly-scoped CD workflow.
- **Keyless beats key-management.** OIDC mints short-lived tokens per run and is
  bindable to this repo + branch, so there is no permanent key to leak or rotate.
  This was the deciding factor given security was the stated priority.

## Considered and rejected

- **Service-account JSON key as a GitHub Secret** for CD. Simpler to wire up, but
  leaves a permanent high-privilege credential in GitHub. Rejected in favour of
  OIDC; the one-time WIF setup cost is small and the risk reduction is permanent.
- **Live `dbt build` in the PR gate.** Rejected: non-deterministic, slow, and
  forces secrets into the high-frequency workflow. That build is exactly what CD
  is for.

## Consequences

- The PR gate does **not** catch warehouse-runtime or SQL-dialect errors (e.g.
  BigQuery `QUALIFY`/`APPROX_QUANTILES` that would fail on another adapter); only
  CD exercises real SQL against BigQuery. `dbt parse` catches `ref`/`source`/Jinja
  errors offline, not execution errors.
- CD depends on four GitHub repo **variables** (not secrets):
  `GCP_PROJECT_ID`, `GCP_LOCATION`, `GCP_WORKLOAD_IDENTITY_PROVIDER`,
  `GCP_SERVICE_ACCOUNT`. One-time GCP setup is in `docs/cicd-setup-guide.md`.
