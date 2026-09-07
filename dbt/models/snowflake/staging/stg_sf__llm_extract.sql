{{
    config(
        materialized='view',
    )
}}

-- Shape the extraction payload into columns, leaving required_skills as an
-- ARRAY. Flattening it here would fix the grain at one row per skill and force
-- every consumer that does not want that to re-aggregate; the marts flatten
-- it themselves, at the point where the wider grain is actually wanted.
--
-- Only rows the extractor succeeded on are exposed. Failures stay in the raw
-- table with their error_message rather than being dropped at ingest, so the
-- failure rate remains measurable.

select
    job_id,
    snapshot_date,

    payload:required_skills                                     as required_skills,
    array_size(coalesce(payload:required_skills, array_construct())) as skills_count,

    payload:years_experience::int                               as years_experience,
    payload:sponsorship_signal::string                          as sponsorship_signal,
    payload:local_experience_required::boolean                  as local_experience_required,
    payload:remote_friendly::string                             as remote_friendly,

    payload:model_version::string                               as model_version,
    try_to_timestamp_ntz(payload:extracted_at::string)          as extracted_at

from {{ source('snowflake_raw', 'adzuna_llm_extract') }}
where payload:extraction_status::string = 'ok'
