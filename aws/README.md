# AWS side of the multi-cloud track

Per [ADR-0002](../docs/adr/0002-multi-cloud-architecture.md), the AWS path runs
alongside the GCP one rather than replacing it. `ingest_adzuna.py` dual-writes
every snapshot to `s3://au-jobs-radar-raw/` as well as GCS; this directory
holds the catalogue that makes those objects queryable.

## Layout

```
aws/glue/
├── 01_database.sql             Glue database au_jobs_radar
├── 02_adzuna_jobs.sql          external table over adzuna/
└── 03_adzuna_llm_extract.sql   external table over adzuna_llm_extract/
```

The two prefixes are siblings, not nested. Their records have entirely
different shapes, and a table rooted at `adzuna/` would otherwise try to read
the extracts with the job schema. `adzuna/` is written by ingest; the extracts
are exported from BigQuery by `publish_extracts_to_lake.py`, because extraction
is incremental and its complete state lives in the warehouse.

The Glue Data Catalog is defined by these files, not by a crawler. A crawler
would guess the schema on a schedule, bill per run, and leave no record of what
it decided. Here the JSON-to-table mapping is explicit, diffable, and
reproducible from an empty account.

## Applying

```bash
uv run --env-file .env python scripts/apply_athena_ddl.py --verify
```

Statements are applied in filename order, one per file, because Athena executes
a single statement per call. `--dry-run` lists them without executing;
`--verify` follows up with a row count per snapshot date.

Requires `ATHENA_OUTPUT_S3` and an identity with the Glue, Athena, and
results-bucket permissions described in `.env.example`.

## Partitions

`snapshot_date` uses **partition projection**: Athena derives the partition list
from the key template in the DDL, so a new snapshot becomes queryable the moment
ingest writes it. There is no crawler to schedule and no `MSCK REPAIR TABLE` to
remember.

Verified working — a query filtered to an existing partition scanned 1,497,121
bytes (exactly the three objects in it), and one filtered to a date with no
objects scanned 0 bytes.

## Athena is Trino, and Trino is not BigQuery

The same logical query is written differently on each engine. These were found
by running both sides and comparing results, not by reading documentation.

| Intent | BigQuery | Athena (Trino) |
|---|---|---|
| Nth element of an array, NULL when short | `location.area[safe_offset(2)]` | `element_at(location.area, 3)` |
| Array indexing base | 0-based | **1-based** |
| Parse an ISO 8601 string | native `TIMESTAMP` column | `from_iso8601_timestamp(created)` |
| Array length | `array_length(x)` | `cardinality(x)` |

The array one bites hardest. BigQuery's `safe_offset` returns NULL past the end
of an array; Trino's `[]` subscript **raises** — `Array subscript must be less
than or equal to array length: 3 > 1`. Exactly one row in the 2026-05-07
snapshot triggers it: job `5696154856`, a national posting whose
`location.area` holds only `["Australia"]` with no state or city. `element_at`
is the tolerant form and matches BigQuery's behaviour, NULL included.

Timestamps are declared `string` rather than `timestamp` in the DDL because
both `created` and `ingested_at` arrive as ISO 8601, which Hive's `timestamp`
does not parse — it would yield NULL silently rather than fail. Converting at
query time is explicit and verifiable.

## Equivalence with BigQuery

Same snapshot, both engines, run 2026-08-10:

| Measure | Athena over S3 | BigQuery |
|---|---|---|
| Rows in `snapshot_date=2026-05-07` | 4,704 | 4,704 |
| Distinct `id` | 3,975 | 3,975 |
| Top cities by count | Sydney 2,806 · Melbourne 1,102 · Brisbane 795 · NULL 1 | identical |
| `min`/`max` of `created` | 2026-04-07 03:42:12 / 2026-05-07 11:09:00 | identical |
| Extract rows / distinct `job_id` | 3,975 / 3,975 | identical |
| Top skills | AWS 107 · Azure 52 · Salesforce 47 · SAP 26 · AI 23 | identical |

The row-vs-distinct gap is the known cross-city duplication documented in
`stg_adzuna__jobs`: Adzuna's `where` filter is fuzzy, so a national or remote
posting surfaces in several per-city ingestion runs.

## Cost

The whole table is 1.4 MB. Athena bills $5/TB scanned with a 10 MB minimum per
query, so a full scan costs about $0.00005. The Glue Data Catalog is free below
one million objects.
