{{
    config(
        materialized='view',
    )
}}

with source_data as (

    select * from {{ source('raw', 'occupation_lists') }}

),

normalised as (

    select
        anzsco_code,
        nullif(trim(occupation_name), '')           as occupation_name,
        upper(trim(list_name))                      as list_name,
        effective_date,

        upper(trim(list_name)) = 'MLTSSL'           as is_mltssl,
        upper(trim(list_name)) = 'STSOL'            as is_stsol,
        upper(trim(list_name)) = 'CSOL'             as is_csol

    from source_data

)

select * from normalised
