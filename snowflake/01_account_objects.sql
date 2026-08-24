-- Account-level objects for the multi-cloud track (ADR-0002).
--
-- Run once, as ACCOUNTADMIN, in a Snowsight worksheet immediately after the
-- account is created. Everything after this point runs as DBT_ROLE.
--
-- Names match what `.env.example` and `dbt/profiles.yml` already expect, so a
-- fresh trial account drops straight into the existing configuration:
--   database AU_JOBS_RADAR, schemas RAW / STAGING / MARTS,
--   warehouse COMPUTE_WH, role DBT_ROLE.
--
-- Snowsight does NOT carry session context across separate Run clicks, so
-- every statement is fully qualified. Select the whole worksheet and run it in
-- one go rather than clicking through statement by statement.

USE ROLE ACCOUNTADMIN;

-- Extra-small, and asleep within a minute of going idle. The dataset is 1.4 MB;
-- the entire dbt build is seconds of compute, and the trial credit only has to
-- survive 30 days.
CREATE WAREHOUSE IF NOT EXISTS COMPUTE_WH
    WAREHOUSE_SIZE     = 'XSMALL'
    AUTO_SUSPEND       = 60
    AUTO_RESUME        = TRUE
    INITIALLY_SUSPENDED = TRUE
    COMMENT = 'AU Jobs Radar — sized for a 1.4 MB dataset, not for throughput';

CREATE DATABASE IF NOT EXISTS AU_JOBS_RADAR
    COMMENT = 'AU Migration & Jobs Radar — Snowflake side of the multi-cloud track';

-- Mirrors the BigQuery dataset layout so the two warehouses stay legible to
-- the same reader, even though their models are written independently.
CREATE SCHEMA IF NOT EXISTS AU_JOBS_RADAR.RAW
    COMMENT = 'VARIANT-native landing layer, loaded from the S3 external stage';
CREATE SCHEMA IF NOT EXISTS AU_JOBS_RADAR.STAGING
    COMMENT = 'dbt staging and intermediate models';
CREATE SCHEMA IF NOT EXISTS AU_JOBS_RADAR.MARTS
    COMMENT = 'dbt marts';

CREATE ROLE IF NOT EXISTS DBT_ROLE
    COMMENT = 'Role used by dbt and by the loader; not ACCOUNTADMIN';

GRANT USAGE, OPERATE ON WAREHOUSE COMPUTE_WH TO ROLE DBT_ROLE;
GRANT USAGE ON DATABASE AU_JOBS_RADAR TO ROLE DBT_ROLE;
GRANT ALL PRIVILEGES ON SCHEMA AU_JOBS_RADAR.RAW TO ROLE DBT_ROLE;
GRANT ALL PRIVILEGES ON SCHEMA AU_JOBS_RADAR.STAGING TO ROLE DBT_ROLE;
GRANT ALL PRIVILEGES ON SCHEMA AU_JOBS_RADAR.MARTS TO ROLE DBT_ROLE;

-- CREATE STAGE on RAW is covered by ALL PRIVILEGES above; the storage
-- integration itself is created by ACCOUNTADMIN in 02 and granted separately,
-- because integrations are account-level objects.

-- A service identity separate from the human login. dbt and the Python
-- connector authenticate as this user, never as the account administrator who
-- signs in to Snowsight. Three reasons: MFA on a human account blocks
-- programmatic connections outright; a script should not run with ACCOUNTADMIN
-- privileges; and rotating this credential must not lock the owner out of the
-- console.
--
-- Replace <SERVICE_PASSWORD> before running, and keep it only in `.env`.
CREATE USER IF NOT EXISTS DBT_USER
    PASSWORD          = '<SERVICE_PASSWORD>'
    DEFAULT_ROLE      = DBT_ROLE
    DEFAULT_WAREHOUSE = COMPUTE_WH
    DEFAULT_NAMESPACE = AU_JOBS_RADAR.STAGING
    MUST_CHANGE_PASSWORD = FALSE
    COMMENT = 'Service identity for dbt and the S3 loader; not a person';

GRANT ROLE DBT_ROLE TO USER DBT_USER;

-- Also grant the role to the human login, so the same objects can be inspected
-- in Snowsight without switching to ACCOUNTADMIN. Replace with your username
-- from `select current_user()`.
GRANT ROLE DBT_ROLE TO USER <YOUR_LOGIN>;

-- Trial-credit guard. Suspends the warehouse rather than merely emailing, so a
-- runaway query cannot quietly consume the 30-day allowance overnight.
CREATE RESOURCE MONITOR IF NOT EXISTS AU_JOBS_RADAR_MONITOR
    WITH CREDIT_QUOTA = 20
    FREQUENCY = MONTHLY
    START_TIMESTAMP = IMMEDIATELY
    TRIGGERS
        ON 50  PERCENT DO NOTIFY
        ON 80  PERCENT DO NOTIFY
        ON 100 PERCENT DO SUSPEND
        ON 110 PERCENT DO SUSPEND_IMMEDIATE;

ALTER WAREHOUSE COMPUTE_WH SET RESOURCE_MONITOR = AU_JOBS_RADAR_MONITOR;
