---
status: accepted
---

# Multi-cloud by division of labour, not by SQL portability

## Decision

Phase 2 adds AWS and Snowflake as a **second, independent path** alongside the
existing GCP path, rather than making one dbt project run on several warehouses.

- **GCP path (unchanged, production):** Adzuna → GCS → BigQuery → dbt → Power BI,
  rebuilt monthly by the keyless OIDC CD workflow of ADR-0001.
- **AWS path (new):** `scripts/ingest_adzuna.py` **dual-writes** each snapshot to
  S3 as well as GCS. A Glue Data Catalog table (hand-written DDL, no crawler)
  sits over the S3 prefix; Athena queries the lake in place.
- **Snowflake (new):** hosted on **AWS, ap-southeast-2**, reading the same S3
  prefix through a `STORAGE INTEGRATION` + `EXTERNAL STAGE`. Its raw layer is
  written **natively against `VARIANT`** — it is not a port of the BigQuery
  models, and there is **no `{% if target.type %}` branching and no
  `adapter.dispatch` macro layer** anywhere in the dbt project.

**Build order is fixed:** AWS minimal set (S3 + IAM role + Glue DDL + Athena)
→ *then* create the Snowflake account → *then*, optionally, serverless ingest
on EventBridge + Lambda.

## Why

- **Portability would forfeit the thing being demonstrated.** Running one SQL
  codebase on two warehouses constrains every model to the *intersection* of the
  two dialects, which means giving up `VARIANT`-native modelling, `LATERAL
  FLATTEN`, dynamic tables, clustering keys, and warehouse/credit tuning —
  precisely the Snowflake-specific vocabulary the track exists to build. The
  goal is demonstrable Snowflake competence, not dialect-agnostic SQL.
- **This is the shape the industry actually runs.** Object storage as the
  landing zone with Snowflake reading it via an external stage is among the most
  common real Snowflake deployments. "The same dbt project runs on two
  warehouses" is not a shape enterprises operate; what they do is *migrate*
  (one-time, directional) or *divide work across clouds* (structural).
- **Sequencing is dictated by the only clock in the plan.** The Snowflake trial
  is 30 days. AWS credits run 6 months and its Always Free tier has no expiry;
  GCP is a paid account whose 7.67 MB footprint sits inside BigQuery's always-free
  allowance and costs ~$0/month. AWS infrastructure therefore has to be finished
  *before* the Snowflake clock starts, or trial days get spent configuring IAM.
- **The existing object key layout is already lake-compatible.** `build_blob_key`
  in `adzuna_pipeline/storage.py` emits
  `adzuna/snapshot_date=YYYY-MM-DD/<city>.jsonl.gz` — Hive-style partitioning
  that Glue/Athena partition discovery and Snowflake staged-path reads both
  consume unchanged.

## Considered and rejected

- **One dbt project with inline `{% if target.type == 'bigquery' %}` branches**
  (the prior working assumption, carried over from an earlier session). Rejected:
  it becomes three-way nesting once Athena is added, buries business logic inside
  dialect plumbing in the models a reader will actually open, and still imposes
  lowest-common-denominator SQL.
- **An `adapter.dispatch` macro layer** (the mechanism `dbt_utils` itself uses).
  Rejected for the same root reason — it is a good solution to the mechanics of
  portability, but portability is the wrong objective here. Recorded because it
  is the natural suggestion and will otherwise be proposed again.
- **A single lake with BigQuery also reading S3.** Rejected: requires BigQuery
  Omni, which is outside the free allowance and region-restricted.
- **GCS → S3 one-way replication instead of dual-write.** Rejected: an extra
  scheduled component that can fail, buying nothing over writing twice at
  ingest time.
- **Glue Crawler for schema discovery.** Rejected: ~$0.15 per crawl (0.44
  USD/DPU-hour, 10-minute minimum) and opaque; hand-written DDL is cheaper and
  makes the JSON→table mapping explicit.
- **Redshift / EMR / Kinesis.** Rejected: genuine cost, and marginal value for
  AI/data-engineer roles compared with covering Snowflake thoroughly.

## Consequences

- The same ~1.4 MB of raw payload is stored in two object stores. Accepted
  duplication: it keeps the two paths independent, so neither cloud is a single
  point of failure for the other.
- **The two warehouses' staging layers diverge deliberately.** There is no shared
  model layer to keep in sync and no automatic guarantee that BigQuery and
  Snowflake marts agree; any equivalence claim has to be demonstrated, not
  assumed.
- **The Snowflake account must be created on AWS in ap-southeast-2** so that
  reads from S3 stay same-region and incur no egress. Cloud and region are fixed
  at signup and cannot be changed afterwards.
- `scripts/sync_bq_to_snowflake.py` is repurposed rather than discarded: the
  stage + `COPY INTO` logic is retained, while the BigQuery→Arrow→Parquet source
  is replaced by the S3 external stage.
- `adzuna_pipeline/storage.py` grows a second uploader alongside `GcsRawUploader`,
  and `UploadResult.gcs_uri` is renamed to `uri` (three call sites in
  `scripts/ingest_adzuna.py`).
- The GCP CD workflow, the Power BI dashboard, and the marts contract are
  untouched by this decision.
- BigQuery-side SQL cleanups identified while surveying dialect differences —
  `safe_divide(a, b)` → `a / nullif(b, 0)`, the redundant `cast(created as
  timestamp)` / `date(timestamp(created))` on an already-`TIMESTAMP` column, and
  the unnecessary `r''` raw-string prefixes — are plain code quality and proceed
  independently of this ADR.
