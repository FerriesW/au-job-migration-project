{{
    config(
        materialized='table',
        cluster_by=['state'],
    )
}}

-- Q2 fact table. For each (anzsco_code, state) combination, computes the
-- share of postings that explicitly require Australian or local work
-- experience, plus the parallel sponsorship-signal share. Groups with
-- fewer than five postings are filtered out to avoid noisy ratios.

with mapped_jobs as (

    select * from {{ ref('int_jobs_anzsco_mapped') }}
    where anzsco_code is not null
      and state is not null

),

aggregated as (

    select
        anzsco_code,
        state,
        count(*)                                          as total_jobs,
        countif(local_experience_required)                as local_experience_count,
        countif(sponsorship_signal = 'explicit_yes')      as sponsorship_yes_count,
        countif(sponsorship_signal = 'explicit_no')       as sponsorship_no_count
    from mapped_jobs
    group by anzsco_code, state

)

select
    a.anzsco_code,
    o.occupation_name,
    o.list_membership,
    a.state,
    a.total_jobs,
    a.local_experience_count,
    a.sponsorship_yes_count,
    a.sponsorship_no_count,
    safe_divide(a.local_experience_count, a.total_jobs) as local_experience_pct,
    safe_divide(a.sponsorship_yes_count,   a.total_jobs) as sponsorship_yes_pct,
    safe_divide(a.sponsorship_no_count,    a.total_jobs) as sponsorship_no_pct
from aggregated a
left join {{ ref('dim_occupation') }} o using (anzsco_code)
where a.total_jobs >= 5
