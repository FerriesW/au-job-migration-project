{{
    config(
        materialized='view',
    )
}}

with source_data as (

    select * from {{ source('raw', 'eoi_invitations') }}

),

enriched as (

    select
        round_date,
        extract(year  from round_date)              as round_year,
        extract(month from round_date)              as round_month,

        upper(trim(visa_subclass))                  as visa_subclass,
        nullif(trim(anzsco_code), '')               as anzsco_code,

        points_cutoff,
        invitations_issued

    from source_data

)

select * from enriched
