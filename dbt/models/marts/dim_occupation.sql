{{
    config(
        materialized='table',
    )
}}

-- Conformed occupation dimension. Anchored on the union of ANZSCO codes
-- known to the project: those declared on the Home Affairs migration lists
-- and those covered by the title-pattern seed. Each row carries
-- list-membership flags and a human-readable occupation_name.

with from_lists as (

    select
        anzsco_code,
        occupation_name,
        list_name,
        effective_date
    from {{ ref('stg_occupation_lists') }}

),

from_patterns as (

    select distinct
        anzsco_code,
        first_value(occupation_name) over (
            partition by anzsco_code order by priority
        ) as occupation_name
    from {{ ref('anzsco_title_patterns') }}

),

all_codes as (

    select anzsco_code, occupation_name from from_lists
    union distinct
    select anzsco_code, occupation_name from from_patterns

),

resolved_names as (

    select
        anzsco_code,
        any_value(occupation_name) as occupation_name
    from all_codes
    group by anzsco_code

),

membership as (

    select
        anzsco_code,
        max(if(list_name = 'MLTSSL', true, false)) as is_mltssl,
        max(if(list_name = 'STSOL',  true, false)) as is_stsol,
        max(if(list_name = 'CSOL',   true, false)) as is_csol,
        string_agg(distinct list_name order by list_name) as list_membership,
        max(effective_date) as effective_date
    from from_lists
    group by anzsco_code

)

select
    n.anzsco_code,
    n.occupation_name,
    coalesce(m.is_mltssl, false) as is_mltssl,
    coalesce(m.is_stsol,  false) as is_stsol,
    coalesce(m.is_csol,   false) as is_csol,
    m.list_membership,
    m.effective_date
from resolved_names n
left join membership m using (anzsco_code)
