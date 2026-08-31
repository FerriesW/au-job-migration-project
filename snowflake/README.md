# Snowflake side of the multi-cloud track

Per [ADR-0002](../docs/adr/0002-multi-cloud-architecture.md), Snowflake reads
the **same S3 objects** Athena reads, through a storage integration and external
stage, and models them independently of the BigQuery project. There is no shared
dbt model layer and no `target.type` branching anywhere.

## What Snowflake is doing here, stated plainly

Functionally it is redundant. BigQuery is already the warehouse and Athena
already queries the lake; this is a third engine over 4,704 rows. It is a
deliberate comparison exercise, not an architectural necessity, and it earns its
place by showing the one thing neither alternative can: a warehouse that is not
owned by the cloud its data sits in, with semi-structured data as a first-class
type rather than a schema you have to declare in advance.

## Apply order

```
01_account_objects.sql      warehouse, database, schemas, DBT_ROLE, DBT_USER, resource monitor
02_storage_integration.sql  trust with AWS — two passes, see the file header
03_stage_and_format.sql     external stage over s3://au-jobs-radar-raw/adzuna/
04_raw_variant_table.sql    VARIANT landing table + COPY INTO + equivalence check
05_llm_extract.sql          second stage and VARIANT table for the LLM extracts
```

01 and 02 run as `ACCOUNTADMIN` in a Snowsight worksheet; 03 to 05 run as
`DBT_ROLE`. 05 has an `ACCOUNTADMIN` prerequisite in its header: the storage
integration is scoped by prefix and the extract dataset lives in a sibling one,
so it has to be admitted explicitly. Snowsight does not carry session context between separate Run
clicks, so each file is fully qualified — select the whole worksheet and run it
in one go.

`02` contains a placeholder for the AWS role ARN. It embeds the AWS account id,
which is deliberately kept out of this repository.

## Cost discipline on a 30-day trial

The warehouse is `XSMALL` with `AUTO_SUSPEND = 60`, and a resource monitor
**suspends** it at 20 credits rather than merely emailing. Budget alerts on GCP
and AWS only notify; Snowflake can actually stop, and a trial allowance that a
runaway query drains overnight cannot be recovered.

## VARIANT versus declared schema — the same bytes, two philosophies

| | Glue / Athena | Snowflake |
|---|---|---|
| Schema | every column declared in DDL | one `VARIANT` column |
| A new Adzuna field | invisible until the DDL is edited | queryable immediately |
| Reading a nested value | `location.display_name` after declaring the struct | `payload:location:display_name::string` |
| Cost of being wrong | re-run the DDL, reload nothing | none — the record was never reshaped |

Only the fields used for filtering and lineage are lifted out and typed
(`snapshot_date`, `source_city`, `ingested_at`, `stage_file`). Everything else
stays in the payload and is shaped downstream in dbt.

This makes schema-on-read literal. Nothing declares the Adzuna fields, yet:

```sql
select f.key, count(*)
from AU_JOBS_RADAR.RAW.ADZUNA_JOBS, lateral flatten(input => payload) f
group by 1 order by 2 desc;
```

enumerates every top-level field in the data, including Adzuna's internal
`__CLASS__` markers that the Glue table silently drops.

## Two datasets, and where each comes from

| Prefix | Written by | Path |
|---|---|---|
| `adzuna/` | `ingest_adzuna.py` | Adzuna → both landing zones → BigQuery |
| `adzuna_llm_extract/` | `publish_extracts_to_lake.py` | Qwen → BigQuery → both landing zones |

The second runs the other way round on purpose. Extraction is incremental —
only jobs lacking a result are processed, then MERGEd — so the complete set
lives in the warehouse and writing just the latest batch to the lake would
overwrite an object with a fraction of its own content. The table is exported
whole instead, one object per snapshot date.

**The consequence, stated plainly: for derived data, the AWS and Snowflake
paths depend on BigQuery.** Only the raw path is genuinely independent across
clouds. That is a real limit of this architecture, not a detail.

## Three engines, one dataset

Same `snapshot_date = 2026-05-07`, verified 2026-08-10:

| Measure | BigQuery | Athena (Trino) | Snowflake |
|---|---|---|---|
| Raw rows | 4,704 | 4,704 | 4,704 |
| Distinct job `id` | 3,975 | 3,975 | 3,975 |
| Sydney / Melbourne / Brisbane / NULL | 2,806 / 1,102 / 795 / 1 | identical | identical |
| Extract rows | 3,975 | 3,975 | 3,975 |
| Top skills | AWS 107 · Azure 52 · Salesforce 47 · SAP 26 · AI 23 | identical | identical |
| Sponsorship signal | explicit_no 99 · explicit_yes 2 · unspecified 3,874 | identical | identical |

These numbers are not transcribed. `scripts/compare_engines.py` regenerates
them by asking each engine the same question in its own dialect, and exits
non-zero if any two disagree; the questions live in
`adzuna_pipeline/equivalence.py`, each spelled three ways side by side.

### What the same questions cost

Over those five checks, run 2026-08-24:

| Engine | Bytes scanned | Billing model |
|---|---|---|
| BigQuery | 618,900 | per byte scanned, 1 TiB/month free |
| Athena | 3,171,347 | per byte scanned, $5/TB, 10 MB minimum per query |
| Snowflake | not exposed per query | per credit-second of warehouse uptime |

Athena reads five times as much for identical answers. That is the storage
format, not the question: BigQuery reads only the columns a query names, while
Athena has to decompress and parse the whole JSON object every time. It is the
clearest argument in this project for converting a landing zone to Parquet
before anyone queries it seriously — and the reason the difference is worth
measuring rather than assuming.

### Same array, three idioms

| | Reaching into `required_skills` |
|---|---|
| BigQuery | `, unnest(required_skills) skill` |
| Athena (Trino) | `cross join unnest(required_skills) as t(s)` |
| Snowflake | `, lateral flatten(input => payload:required_skills) f` |

Snowflake's reads the array out of a VARIANT that was never declared; the other
two read a column that had to be.

### Array subscripting is where they disagree

| | BigQuery | Athena (Trino) | Snowflake |
|---|---|---|---|
| Base | 0 | **1** | 0 |
| Out of bounds | NULL | **raises** | NULL |
| Third element | `location.area[safe_offset(2)]` | `element_at(location.area, 3)` | `payload:location:area[2]` |

Trino is the odd one out on both counts. One row in this snapshot proves it:
job `5696154856`, a national posting whose `location.area` is just
`["Australia"]`. BigQuery and Snowflake return NULL for it; Trino's `[]`
subscript fails the whole query with `Array subscript must be less than or equal
to array length: 3 > 1`. That single row is why the Athena queries use
`element_at`.

None of this came from reading documentation. It came from running the same
aggregate on all three and comparing the output.
