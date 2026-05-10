{{
    config(
        materialized='table',
        partition_by={
            "field": "snapshot_date",
            "data_type": "date",
            "granularity": "day",
        },
        cluster_by=["state", "sponsorship_signal", "remote_friendly"],
    )
}}

with jobs as (

    select * from {{ ref('stg_adzuna__jobs') }}

),

extracts as (

    select
        job_id,
        snapshot_date,
        required_skills,
        years_experience,
        sponsorship_signal,
        local_experience_required,
        remote_friendly,
        model_version       as extraction_model_version,
        extracted_at        as extraction_extracted_at,
        extraction_status
    from {{ source('staging', 'adzuna_jobs_llm_extract') }}
    where extraction_status = 'ok'

),

joined as (

    select
        -- Identifiers / partition keys
        j.job_id,
        j.snapshot_date,
        j.source_city,

        -- Job descriptors
        j.job_title,
        j.company_name,
        j.category_tag,
        j.category_label,

        -- Geography
        j.location_display,
        j.country,
        j.state,
        j.city,
        j.suburb,
        j.latitude,
        j.longitude,

        -- Posting metadata
        j.posted_at,
        j.posted_date,

        -- Salary (Adzuna model-predicted; treat as approximate)
        j.salary_min,
        j.salary_max,
        j.salary_avg,
        j.salary_is_predicted,
        j.contract_type,
        j.contract_time,

        -- LLM-extracted structured signals
        e.required_skills,
        e.years_experience,
        e.sponsorship_signal,
        e.local_experience_required,
        e.remote_friendly,

        -- Coverage flags for downstream filtering
        e.job_id is not null                                   as has_extraction,
        array_length(coalesce(e.required_skills, []))          as skills_count,

        -- Source description retained for ad-hoc inspection and re-extraction
        j.description_text,

        -- Lineage / audit fields
        e.extraction_model_version,
        e.extraction_extracted_at

    from jobs j
    left join extracts e
        on e.job_id = j.job_id
       and e.snapshot_date = j.snapshot_date

)

select * from joined
