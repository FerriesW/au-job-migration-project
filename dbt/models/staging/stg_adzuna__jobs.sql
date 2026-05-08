{{
    config(
        materialized='view',
    )
}}

-- Adzuna's `where` filter is a fuzzy match; the same job posting commonly
-- surfaces in multiple per-city ingestion runs (typically remote / national
-- roles, observed at ~15% of rows). Deduplicate on (id, snapshot_date) and
-- keep the row whose source_city sorts first to make the choice deterministic.
-- The authoritative geographic attribution comes from location.area, not
-- source_city.

with source_data as (

    select * from {{ source('raw', 'adzuna_jobs') }}
    qualify row_number() over (
        partition by id, snapshot_date
        order by source_city
    ) = 1

),

flattened as (

    select
        id                                                          as job_id,
        snapshot_date,
        ingested_at,
        source_city,

        nullif(trim(title), '')                                     as job_title,
        nullif(trim(company.display_name), '')                      as company_name,

        location.display_name                                       as location_display,
        location.area[safe_offset(0)]                               as country,
        location.area[safe_offset(1)]                               as state,
        location.area[safe_offset(2)]                               as city,
        location.area[safe_offset(3)]                               as suburb,

        category.tag                                                as category_tag,
        category.label                                              as category_label,

        cast(created as timestamp)                                  as posted_at,
        date(timestamp(created))                                    as posted_date,

        salary_min,
        salary_max,
        case
            when salary_min is not null and salary_max is not null
                then (salary_min + salary_max) / 2
            when salary_min is not null then salary_min
            when salary_max is not null then salary_max
            else null
        end                                                         as salary_avg,
        salary_is_predicted,

        contract_type,
        contract_time,
        latitude,
        longitude,
        redirect_url,
        adref,

        -- Strip HTML tags and collapse whitespace from the truncated description.
        trim(regexp_replace(
            regexp_replace(coalesce(description, ''), r'<[^>]+>', ' '),
            r'\s+',
            ' '
        ))                                                          as description_text

    from source_data

)

select * from flattened
