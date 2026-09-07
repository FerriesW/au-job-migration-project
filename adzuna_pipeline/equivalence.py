"""The questions all three engines must answer identically, and their dialects.

ADR-0002 claims BigQuery, Athena and Snowflake agree over the same data. Until
now that claim lived in prose in three READMEs, backed by throwaway scripts.
This module is the claim itself, in a form that can be re-run.

Each check states one question three ways. Keeping the three spellings side by
side makes the dialect differences the subject rather than a footnote — the
`location.area` row alone shows BigQuery's `safe_offset`, Trino's `element_at`
and Snowflake's VARIANT path, and why the first two are not interchangeable.

Adding a check is the only thing needed to widen the guarantee; the runner and
the comparison logic do not change.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final

SNAPSHOT: Final[str] = "2026-05-07"


@dataclass(frozen=True)
class EquivalenceCheck:
    """One question, spelled for each engine.

    Attributes:
        name: Human-readable description, used in reports.
        why: What a disagreement here would mean — recorded so a future failure
            is diagnosable without re-deriving the intent.
        bigquery: The question in BigQuery SQL.
        athena: The same question in Athena (Trino) SQL.
        snowflake: The same question in Snowflake SQL.
    """

    name: str
    why: str
    bigquery: str
    athena: str
    snowflake: str


CHECKS: Final[tuple[EquivalenceCheck, ...]] = (
    EquivalenceCheck(
        name="raw row and distinct-id counts",
        why=(
            "The most basic claim: the S3 objects Athena and Snowflake read hold "
            "the same postings BigQuery was loaded with. The gap between the two "
            "numbers is Adzuna's cross-city duplication, not a load defect."
        ),
        bigquery=f"""
            select count(*), count(distinct id)
            from `{{project}}.raw.adzuna_jobs`
            where snapshot_date = '{SNAPSHOT}'
        """,
        athena=f"""
            select count(*), count(distinct id)
            from au_jobs_radar.adzuna_jobs
            where snapshot_date = '{SNAPSHOT}'
        """,
        snowflake=f"""
            select count(*), count(distinct payload:id::string)
            from AU_JOBS_RADAR.RAW.ADZUNA_JOBS
            where snapshot_date = '{SNAPSHOT}'
        """,
    ),
    EquivalenceCheck(
        name="city histogram from the nested location array",
        why=(
            "Array subscripting is where the engines genuinely disagree. One "
            "posting (job 5696154856) is national and carries only "
            "['Australia'], so BigQuery and Snowflake return NULL for its city "
            "while Trino's bare [] subscript raises and fails the whole query. "
            "Trino also indexes from 1, not 0. If this check ever passes with "
            "Trino using [3] instead of element_at, the data has changed."
        ),
        bigquery=f"""
            select location.area[safe_offset(2)] as city, count(*) as n
            from `{{project}}.raw.adzuna_jobs`
            where snapshot_date = '{SNAPSHOT}'
            group by 1 order by 2 desc, 1
        """,
        athena=f"""
            select element_at(location.area, 3) as city, count(*) as n
            from au_jobs_radar.adzuna_jobs
            where snapshot_date = '{SNAPSHOT}'
            group by 1 order by 2 desc, 1
        """,
        snowflake=f"""
            select payload:location:area[2]::string as city, count(*) as n
            from AU_JOBS_RADAR.RAW.ADZUNA_JOBS
            where snapshot_date = '{SNAPSHOT}'
            group by 1 order by 2 desc, 1
        """,
    ),
    EquivalenceCheck(
        name="LLM extract row and distinct-job counts",
        why=(
            "Extracts reach the lake by export from BigQuery rather than at "
            "ingest, so this is the check that the export is complete rather "
            "than a partial batch."
        ),
        bigquery=f"""
            select count(*), count(distinct job_id)
            from `{{project}}.staging.adzuna_jobs_llm_extract`
            where snapshot_date = '{SNAPSHOT}'
        """,
        athena=f"""
            select count(*), count(distinct job_id)
            from au_jobs_radar.adzuna_llm_extract
            where snapshot_date = '{SNAPSHOT}'
        """,
        snowflake=f"""
            select count(*), count(distinct job_id)
            from AU_JOBS_RADAR.RAW.ADZUNA_LLM_EXTRACT
            where snapshot_date = '{SNAPSHOT}'
        """,
    ),
    EquivalenceCheck(
        name="top skills, unnesting the extracted array",
        why=(
            "Three idioms for the same operation: UNNEST as a comma join, "
            "CROSS JOIN UNNEST with an alias, and LATERAL FLATTEN over a "
            "VARIANT path that was never declared as a column."
        ),
        bigquery=f"""
            select skill, count(*) as mentions
            from `{{project}}.staging.adzuna_jobs_llm_extract`, unnest(required_skills) skill
            where snapshot_date = '{SNAPSHOT}'
            group by 1 order by 2 desc, 1 limit 10
        """,
        athena=f"""
            select s as skill, count(*) as mentions
            from au_jobs_radar.adzuna_llm_extract
            cross join unnest(required_skills) as t(s)
            where snapshot_date = '{SNAPSHOT}'
            group by 1 order by 2 desc, 1 limit 10
        """,
        snowflake=f"""
            select f.value::string as skill, count(*) as mentions
            from AU_JOBS_RADAR.RAW.ADZUNA_LLM_EXTRACT,
                 lateral flatten(input => payload:required_skills) f
            where snapshot_date = '{SNAPSHOT}'
            group by 1 order by 2 desc, 1 limit 10
        """,
    ),
    EquivalenceCheck(
        name="skills-demand mart, reached three different ways",
        why=(
            "The strongest check here, because the three sides do not share a "
            "lineage. BigQuery's mart is built through int_jobs_enriched and "
            "int_jobs_anzsco_mapped, including a join to the ANZSCO title "
            "patterns; Snowflake's joins its two staging views directly; the "
            "Athena side recomputes the whole thing inline from the two Glue "
            "tables. Agreement is therefore evidence about the modelling, not "
            "just about the load. Note the Athena spelling filters to one "
            "snapshot while the two marts are single-snapshot by construction "
            "— a second snapshot landing will break this check, which is the "
            "correct moment to give the marts a snapshot dimension."
        ),
        bigquery="""
            select state, skill, mention_count
            from `{project}.marts.fct_skills_demand`
            order by state, skill
        """,
        athena=f"""
            with deduped as (
                select id, location, snapshot_date, source_city,
                       row_number() over (
                           partition by id, snapshot_date order by source_city
                       ) as rn
                from au_jobs_radar.adzuna_jobs
                where snapshot_date = '{SNAPSHOT}'
            ),
            jobs as (
                select id, snapshot_date, source_city,
                       element_at(location.area, 2) as state
                from deduped where rn = 1
            ),
            joined as (
                select j.state, j.source_city, e.required_skills
                from jobs j
                join au_jobs_radar.adzuna_llm_extract e
                  on e.job_id = j.id and e.snapshot_date = j.snapshot_date
                where j.state is not null
                  and e.extraction_status = 'ok'
                  and e.snapshot_date = '{SNAPSHOT}'
            )
            select state, lower(trim(s)) as skill, count(*) as mention_count
            from joined cross join unnest(required_skills) as t(s)
            where length(trim(s)) > 0
            group by state, lower(trim(s))
            having count(*) >= 3
            order by state, 2
        """,
        snowflake="""
            select state, skill, mention_count
            from AU_JOBS_RADAR.MARTS.FCT_SF__SKILLS_DEMAND
            order by state, skill
        """,
    ),
    EquivalenceCheck(
        name="sponsorship signal breakdown",
        why="A plain categorical aggregate, included as a control on the harder checks.",
        bigquery=f"""
            select sponsorship_signal, count(*) as n
            from `{{project}}.staging.adzuna_jobs_llm_extract`
            where snapshot_date = '{SNAPSHOT}'
            group by 1 order by 1
        """,
        athena=f"""
            select sponsorship_signal, count(*) as n
            from au_jobs_radar.adzuna_llm_extract
            where snapshot_date = '{SNAPSHOT}'
            group by 1 order by 1
        """,
        snowflake=f"""
            select payload:sponsorship_signal::string, count(*) as n
            from AU_JOBS_RADAR.RAW.ADZUNA_LLM_EXTRACT
            where snapshot_date = '{SNAPSHOT}'
            group by 1 order by 1
        """,
    ),
)
