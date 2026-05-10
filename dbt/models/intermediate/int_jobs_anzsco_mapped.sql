{{
    config(
        materialized='view',
    )
}}

-- Title-pattern mapping from job_title to ANZSCO code. The seed
-- ``anzsco_title_patterns`` carries (priority, anzsco_code, title_pattern)
-- triples; for each job, the lowest-priority matching pattern wins. Jobs
-- whose title matches no pattern carry a NULL anzsco_code and are
-- attributed to an "unmapped" bucket downstream.

with jobs as (

    select * from {{ ref('int_jobs_enriched') }}

),

patterns as (

    select * from {{ ref('anzsco_title_patterns') }}

),

joined as (

    select
        j.*,
        p.anzsco_code,
        p.priority
    from jobs j
    left join patterns p
        on regexp_contains(lower(j.job_title), p.title_pattern)
    qualify row_number() over (
        partition by j.job_id, j.snapshot_date
        order by p.priority nulls last
    ) = 1

)

select * except(priority)
from joined
