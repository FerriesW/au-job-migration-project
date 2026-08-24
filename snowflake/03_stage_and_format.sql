-- External stage over the S3 landing zone, plus the file format it reads.
--
-- Runs as DBT_ROLE: the integration was granted to it in 02, and the RAW schema
-- privileges in 01 cover CREATE STAGE.
--
-- Nothing is copied by creating a stage. It is a pointer plus a credential —
-- the objects stay in S3, byte-identical to their GCS mirrors, and Snowflake
-- reads them in place until COPY INTO is asked to materialise them.

CREATE FILE FORMAT IF NOT EXISTS AU_JOBS_RADAR.RAW.JSONL_GZ
    TYPE = JSON
    COMPRESSION = GZIP
    -- The landing zone is newline-delimited JSON: one object per line, not one
    -- array per file. Stripping an outer array would collapse each file into a
    -- single malformed record.
    STRIP_OUTER_ARRAY = FALSE;

CREATE STAGE IF NOT EXISTS AU_JOBS_RADAR.RAW.S3_ADZUNA
    STORAGE_INTEGRATION = AU_JOBS_RADAR_S3
    URL                 = 's3://au-jobs-radar-raw/adzuna/'
    FILE_FORMAT         = AU_JOBS_RADAR.RAW.JSONL_GZ;

-- Smoke test: this lists the objects and their MD5s, which should match what
-- `scripts/sync_gcs_to_s3.py` reports for the same keys.
LIST @AU_JOBS_RADAR.RAW.S3_ADZUNA;
