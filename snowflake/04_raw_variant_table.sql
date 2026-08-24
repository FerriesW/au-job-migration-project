-- VARIANT-native raw layer, loaded from the S3 external stage.
--
-- This is the point of the Snowflake track, and the deliberate opposite of what
-- `aws/glue/02_adzuna_jobs.sql` does with the same bytes.
--
-- Glue declares every column up front: a new Adzuna field is invisible until
-- someone edits the DDL. Here the record lands whole in a single VARIANT and is
-- shaped downstream in dbt, so a new field is queryable the moment it appears —
-- `select payload:whatever_is_new` needs no migration.
--
-- Only the fields used for filtering and lineage are lifted out and typed:
-- partition-shaped access should not pay for JSON traversal, and having
-- snapshot_date as a real DATE keeps the predicate sargable.

CREATE TABLE IF NOT EXISTS AU_JOBS_RADAR.RAW.ADZUNA_JOBS (
    payload        VARIANT,
    snapshot_date  DATE,
    source_city    STRING,
    ingested_at    TIMESTAMP_NTZ,
    stage_file     STRING,
    loaded_at      TIMESTAMP_NTZ DEFAULT CURRENT_TIMESTAMP()
);

-- COPY INTO tracks which staged files it has already consumed, by name and
-- ETag, so re-running is idempotent: unchanged files are skipped, and a file
-- rewritten by a later ingest is picked up again.
COPY INTO AU_JOBS_RADAR.RAW.ADZUNA_JOBS
    (payload, snapshot_date, source_city, ingested_at, stage_file)
FROM (
    SELECT $1,
           $1:snapshot_date::DATE,
           $1:source_city::STRING,
           $1:ingested_at::TIMESTAMP_NTZ,
           METADATA$FILENAME
    FROM @AU_JOBS_RADAR.RAW.S3_ADZUNA
)
FILE_FORMAT = (FORMAT_NAME = AU_JOBS_RADAR.RAW.JSONL_GZ);

-- Equivalence check. Must agree with BigQuery `raw.adzuna_jobs` and with the
-- Athena external table over the same S3 objects: 4,704 rows, 3,975 distinct
-- ids for snapshot_date 2026-05-07. The gap between the two is the documented
-- cross-city duplication in Adzuna's fuzzy `where` filter, not a load defect.
SELECT COUNT(*)                             AS rows_loaded,
       COUNT(DISTINCT payload:id::STRING)   AS distinct_ids,
       MIN(snapshot_date)                   AS earliest_snapshot,
       MAX(snapshot_date)                   AS latest_snapshot
FROM AU_JOBS_RADAR.RAW.ADZUNA_JOBS;
