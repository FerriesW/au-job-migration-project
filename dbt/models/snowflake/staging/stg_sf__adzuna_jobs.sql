{{
    config(
        materialized='view',
    )
}}

-- Shape the VARIANT payload into columns. This is not a port of
-- stg_adzuna__jobs: that model reads a table whose columns BigQuery already
-- knows about, while this one does the shredding itself, at read time, from a
-- record nothing has declared.
--
-- Adzuna's `where` filter is fuzzy, so the same posting surfaces in several
-- per-city ingestion runs — about 15% of rows. Deduplicate on
-- (id, snapshot_date), keeping the row whose source_city sorts first so the
-- choice is deterministic. Authoritative geography comes from location.area,
-- not from source_city.
--
-- Snowflake's array subscript is 0-based and returns NULL past the end, which
-- matches BigQuery's safe_offset. Trino is the outlier on both counts, and one
-- national posting in this data proves it — see aws/README.md.

with source_data as (

    select
        payload,
        snapshot_date,
        source_city
    from {{ source('snowflake_raw', 'adzuna_jobs') }}
    qualify row_number() over (
        partition by payload:id::string, snapshot_date
        order by source_city
    ) = 1

),

shredded as (

    select
        payload:id::string                                      as job_id,
        snapshot_date,
        source_city,

        nullif(trim(payload:title::string), '')                 as job_title,
        nullif(trim(payload:company:display_name::string), '')  as company_name,

        payload:location:display_name::string                   as location_display,
        payload:location:area[0]::string                        as country,
        payload:location:area[1]::string                        as state,
        payload:location:area[2]::string                        as city,
        payload:location:area[3]::string                        as suburb,

        payload:category:tag::string                            as category_tag,
        payload:category:label::string                          as category_label,

        -- `created` is ISO 8601 in the payload. try_to_timestamp_ntz returns
        -- NULL rather than failing the whole model if Adzuna ever changes the
        -- format, which is the behaviour a landing layer wants.
        try_to_timestamp_ntz(payload:created::string)           as posted_at,
        try_to_date(payload:created::string)                    as posted_date,

        payload:salary_min::float                               as salary_min,
        payload:salary_max::float                               as salary_max,
        case
            when payload:salary_min is not null and payload:salary_max is not null
                then (payload:salary_min::float + payload:salary_max::float) / 2
            when payload:salary_min is not null then payload:salary_min::float
            when payload:salary_max is not null then payload:salary_max::float
            else null
        end                                                     as salary_avg,
        payload:salary_is_predicted::string                     as salary_is_predicted,

        payload:contract_type::string                           as contract_type,
        payload:contract_time::string                           as contract_time,
        payload:latitude::float                                 as latitude,
        payload:longitude::float                                as longitude,
        payload:redirect_url::string                            as redirect_url,
        payload:adref::string                                   as adref,

        -- Strip HTML tags and collapse whitespace. Snowflake's regexp is POSIX,
        -- so character classes are spelled [[:space:]] rather than \s.
        trim(regexp_replace(
            regexp_replace(coalesce(payload:description::string, ''), '<[^>]+>', ' '),
            '[[:space:]]+',
            ' '
        ))                                                      as description_text

    from source_data

)

select * from shredded
