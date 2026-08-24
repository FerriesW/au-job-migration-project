-- LLM extraction results, landed the same way as the raw snapshots.
--
-- Prerequisite, run once as ACCOUNTADMIN: the storage integration is scoped by
-- prefix, and this dataset lives in a sibling one, so it must be admitted
-- explicitly. The matching AWS role policy needs the same widening.
--
--   ALTER STORAGE INTEGRATION AU_JOBS_RADAR_S3
--       SET STORAGE_ALLOWED_LOCATIONS = (
--           's3://au-jobs-radar-raw/adzuna/',
--           's3://au-jobs-radar-raw/adzuna_llm_extract/'
--       );
--
-- Having to make that edit is the least-privilege scoping working as intended:
-- widening the blast radius is a deliberate act, not something a new prefix
-- inherits for free.

CREATE STAGE IF NOT EXISTS AU_JOBS_RADAR.RAW.S3_LLM_EXTRACT
    STORAGE_INTEGRATION = AU_JOBS_RADAR_S3
    URL                 = 's3://au-jobs-radar-raw/adzuna_llm_extract/'
    FILE_FORMAT         = AU_JOBS_RADAR.RAW.JSONL_GZ;

CREATE TABLE IF NOT EXISTS AU_JOBS_RADAR.RAW.ADZUNA_LLM_EXTRACT (
    payload        VARIANT,
    snapshot_date  DATE,
    job_id         STRING,
    stage_file     STRING,
    loaded_at      TIMESTAMP_NTZ DEFAULT CURRENT_TIMESTAMP()
);

-- Only the join and filter keys are lifted out and typed. `required_skills`,
-- the signals and the model version all stay inside the VARIANT and are read
-- with `payload:` paths downstream — the same choice made for the raw table,
-- for the same reason.
COPY INTO AU_JOBS_RADAR.RAW.ADZUNA_LLM_EXTRACT
    (payload, snapshot_date, job_id, stage_file)
FROM (
    SELECT $1,
           $1:snapshot_date::DATE,
           $1:job_id::STRING,
           METADATA$FILENAME
    FROM @AU_JOBS_RADAR.RAW.S3_LLM_EXTRACT
)
FILE_FORMAT = (FORMAT_NAME = AU_JOBS_RADAR.RAW.JSONL_GZ);

-- Equivalence check against BigQuery `staging.adzuna_jobs_llm_extract` and the
-- Athena table over the same objects: 3,975 rows, 3,975 distinct job_ids, and
-- the skill histogram below identical on all three.
SELECT COUNT(*) AS rows_loaded, COUNT(DISTINCT job_id) AS distinct_jobs
FROM AU_JOBS_RADAR.RAW.ADZUNA_LLM_EXTRACT;

-- LATERAL FLATTEN is Snowflake's idiom for the array the other two engines
-- reach with UNNEST. Expected: AWS 107, Azure 52, Salesforce 47, SAP 26, AI 23.
SELECT f.value::STRING AS skill, COUNT(*) AS mentions
FROM AU_JOBS_RADAR.RAW.ADZUNA_LLM_EXTRACT,
     LATERAL FLATTEN(input => payload:required_skills) f
WHERE snapshot_date = '2026-05-07'
GROUP BY 1
ORDER BY 2 DESC, 1
LIMIT 5;
