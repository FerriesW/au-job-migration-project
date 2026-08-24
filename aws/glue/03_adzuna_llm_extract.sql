-- External table over the published LLM extraction results.
--
-- Sibling prefix to the raw snapshots, not a child of them: the two datasets
-- have entirely different shapes, and a table rooted at `adzuna/` would
-- otherwise try to read these records with the job schema.
--
-- Unlike `adzuna/`, this prefix is written by `publish_extracts_to_lake.py`
-- rather than by ingest. Extraction is incremental and its authoritative state
-- lives in the BigQuery MERGE target, so the whole table is exported per
-- snapshot date instead of appending each batch. One object per date, always
-- complete, overwritten in place.
--
-- Same two conventions as `02_adzuna_jobs.sql`:
--   * `extracted_at` is `string`, not `timestamp` — it is ISO 8601, which
--     Hive's `timestamp` yields NULL for rather than rejecting. Convert with
--     `from_iso8601_timestamp()`.
--   * `snapshot_date` is a partition column only, derived from the key, and is
--     also present inside each record; Hive forbids it being both.

CREATE EXTERNAL TABLE IF NOT EXISTS au_jobs_radar.adzuna_llm_extract (
    job_id                    string,
    required_skills           array<string>,
    years_experience          int,
    sponsorship_signal        string,
    local_experience_required boolean,
    remote_friendly           string,
    model_version             string,
    extracted_at              string,
    extraction_status         string,
    error_message             string
)
PARTITIONED BY (snapshot_date string)
ROW FORMAT SERDE 'org.openx.data.jsonserde.JsonSerDe'
STORED AS TEXTFILE
LOCATION 's3://au-jobs-radar-raw/adzuna_llm_extract/'
TBLPROPERTIES (
    'projection.enabled'                     = 'true',
    'projection.snapshot_date.type'          = 'date',
    'projection.snapshot_date.format'        = 'yyyy-MM-dd',
    'projection.snapshot_date.range'         = '2026-05-01,NOW',
    'projection.snapshot_date.interval'      = '1',
    'projection.snapshot_date.interval.unit' = 'DAYS',
    'storage.location.template'              = 's3://au-jobs-radar-raw/adzuna_llm_extract/snapshot_date=${snapshot_date}/'
)
