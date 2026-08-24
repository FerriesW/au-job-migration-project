-- Trust between Snowflake and the S3 landing zone.
--
-- Run as ACCOUNTADMIN; storage integrations are account-level objects.
--
-- This takes two passes, because each side has to name the other:
--
--   1. In AWS, create an IAM role (`snowflake-s3-integration`) whose trust
--      policy is a deliberate placeholder — nobody can assume it yet — and
--      attach a read-only policy over `s3://<bucket>/adzuna/*`.
--   2. Run the statement below with that role's ARN.
--   3. `DESC INTEGRATION` returns STORAGE_AWS_IAM_USER_ARN and
--      STORAGE_AWS_EXTERNAL_ID. Those are Snowflake's real identity.
--   4. Back in AWS, replace the placeholder trust policy with those values.
--
-- The external ID matters: the IAM user Snowflake presents is shared across its
-- customers, so the ARN alone would let any Snowflake tenant assume the role.
-- The external ID is the per-account secret that makes the grant specific.
--
-- Replace <AWS_ROLE_ARN> — it embeds the AWS account id, which is kept out of
-- this repository. The value lives in `docs/` locally, not here.

USE ROLE ACCOUNTADMIN;

CREATE STORAGE INTEGRATION IF NOT EXISTS AU_JOBS_RADAR_S3
    TYPE                      = EXTERNAL_STAGE
    STORAGE_PROVIDER          = 'S3'
    STORAGE_AWS_ROLE_ARN      = '<AWS_ROLE_ARN>'
    ENABLED                   = TRUE
    -- Scoped to the data prefix only. The bucket also holds nothing else
    -- today, but the stage should not widen just because the bucket might.
    STORAGE_ALLOWED_LOCATIONS = ('s3://au-jobs-radar-raw/adzuna/');

GRANT USAGE ON INTEGRATION AU_JOBS_RADAR_S3 TO ROLE DBT_ROLE;

-- Read STORAGE_AWS_IAM_USER_ARN and STORAGE_AWS_EXTERNAL_ID from the output
-- and paste them into the AWS role's trust policy.
DESC INTEGRATION AU_JOBS_RADAR_S3;
