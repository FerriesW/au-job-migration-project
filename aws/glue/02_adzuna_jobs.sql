-- External table over the raw Adzuna snapshots in S3.
--
-- Hand-written rather than crawled. A Glue crawler would guess this schema on
-- a schedule, bill per run, and leave no record of what it decided; the whole
-- point of the exercise is that the JSON-to-table mapping is explicit and
-- reviewable in version control.
--
-- Four decisions worth knowing about:
--
--   1. Nested objects are declared as `struct`, not as `string`. Queries read
--      `location.display_name` directly, matching how the same field is
--      addressed on the BigQuery side. The cost is that a new Adzuna field
--      requires editing this file — which is the intended trade: the schema
--      is a decision, not a discovery.
--
--   2. `created` and `ingested_at` are `string`, not `timestamp`. Both arrive
--      as ISO 8601 (`2026-07-09T10:02:43Z`, `2026-08-05T05:19:52.048857+00:00`)
--      and Hive's `timestamp` expects `yyyy-MM-dd HH:mm:ss`, so declaring them
--      as `timestamp` yields NULL rather than an error. Convert at query time:
--      `from_iso8601_timestamp(created)`.
--
--   3. `snapshot_date` is a partition column only. It also appears inside each
--      JSON record, but Hive forbids a column being both, and the path is the
--      authoritative copy. The in-record field is simply left undeclared.
--
--   4. Partitions come from projection, not from a crawler or `MSCK REPAIR`.
--      Athena derives the partition list from the key template below, so a new
--      snapshot date becomes queryable the moment ingest writes it, with no
--      catalogue maintenance and no per-run cost.
--
-- Adzuna's internal `__CLASS__` markers are undeclared and therefore ignored
-- by the SerDe, both at the top level and inside each nested object.

CREATE EXTERNAL TABLE IF NOT EXISTS au_jobs_radar.adzuna_jobs (
    id                  string,
    title               string,
    description         string,
    created             string,
    redirect_url        string,
    adref               string,
    salary_min          double,
    salary_max          double,
    salary_is_predicted string,
    contract_type       string,
    contract_time       string,
    latitude            double,
    longitude           double,
    location            struct<display_name: string, area: array<string>>,
    company             struct<display_name: string>,
    category            struct<tag: string, label: string>,
    source_city         string,
    ingested_at         string
)
PARTITIONED BY (snapshot_date string)
ROW FORMAT SERDE 'org.openx.data.jsonserde.JsonSerDe'
STORED AS TEXTFILE
LOCATION 's3://au-jobs-radar-raw/adzuna/'
TBLPROPERTIES (
    'projection.enabled'                     = 'true',
    'projection.snapshot_date.type'          = 'date',
    'projection.snapshot_date.format'        = 'yyyy-MM-dd',
    'projection.snapshot_date.range'         = '2026-05-01,NOW',
    'projection.snapshot_date.interval'      = '1',
    'projection.snapshot_date.interval.unit' = 'DAYS',
    'storage.location.template'              = 's3://au-jobs-radar-raw/adzuna/snapshot_date=${snapshot_date}/'
)
