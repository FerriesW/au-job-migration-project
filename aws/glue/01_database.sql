-- Glue Data Catalog database backing the S3 landing zone.
--
-- Applied through Athena by `scripts/apply_athena_ddl.py`. Athena DDL creates
-- the object in the Glue catalog, so this file is the versioned definition of
-- a Glue database even though it is written as SQL.

CREATE DATABASE IF NOT EXISTS au_jobs_radar
COMMENT 'AU Migration & Jobs Radar — external tables over the S3 raw landing zone'
