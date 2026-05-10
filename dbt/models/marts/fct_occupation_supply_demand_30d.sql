{{
    config(
        materialized='table',
        cluster_by=['list_membership'],
    )
}}

-- Q1 fact table. One row per ANZSCO code combining:
--   * supply  — Adzuna job count over the analysis window
--   * demand  — EOI invitations over the most recent rounds (proxy for
--               competition intensity)
--   * derived eoi_per_job_ratio: lower is healthier for visa applicants
--     (more advertised jobs per EOI invite issued).
-- Jobs with no ANZSCO mapping are excluded from per-occupation aggregation
-- but surfaced separately via the unmapped_jobs_count column for ledger
-- completeness.

with mapped_jobs as (

    select * from {{ ref('int_jobs_anzsco_mapped') }}

),

job_aggregates as (

    select
        anzsco_code,
        count(*)                                                 as jobs_count_30d,
        countif(sponsorship_signal = 'explicit_yes')             as sponsorship_yes_count,
        countif(sponsorship_signal = 'explicit_no')              as sponsorship_no_count,
        countif(remote_friendly = 'remote')                      as remote_count,
        countif(remote_friendly = 'hybrid')                      as hybrid_count,
        countif(remote_friendly = 'onsite')                      as onsite_count,
        countif(local_experience_required)                       as local_experience_count,
        avg(salary_avg)                                          as avg_salary,
        approx_quantiles(salary_avg, 100)[offset(50)]            as median_salary
    from mapped_jobs
    where anzsco_code is not null
    group by anzsco_code

),

eoi_aggregates as (

    select
        anzsco_code,
        sum(invitations_issued)                                  as eoi_invitations_recent,
        max(round_date)                                          as eoi_latest_round
    from {{ ref('stg_eoi') }}
    where anzsco_code is not null
    group by anzsco_code

)

select
    o.anzsco_code,
    o.occupation_name,
    o.is_mltssl,
    o.is_stsol,
    o.is_csol,
    o.list_membership,

    coalesce(j.jobs_count_30d, 0)               as jobs_count_30d,
    coalesce(j.sponsorship_yes_count, 0)        as sponsorship_yes_count,
    coalesce(j.sponsorship_no_count, 0)         as sponsorship_no_count,
    coalesce(j.remote_count, 0)                 as remote_count,
    coalesce(j.hybrid_count, 0)                 as hybrid_count,
    coalesce(j.onsite_count, 0)                 as onsite_count,
    coalesce(j.local_experience_count, 0)       as local_experience_count,
    j.avg_salary,
    j.median_salary,

    coalesce(e.eoi_invitations_recent, 0)       as eoi_invitations_recent,
    e.eoi_latest_round,

    safe_divide(
        coalesce(e.eoi_invitations_recent, 0),
        nullif(coalesce(j.jobs_count_30d, 0), 0)
    )                                           as eoi_per_job_ratio

from {{ ref('dim_occupation') }} o
left join job_aggregates j using (anzsco_code)
left join eoi_aggregates e using (anzsco_code)
