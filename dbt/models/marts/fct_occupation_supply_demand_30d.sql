{{
    config(
        materialized='table',
        cluster_by=['list_membership'],
    )
}}

-- Q1 fact table. One row per ANZSCO code combining:
--   * supply       — Adzuna job count over the analysis window (30 days).
--   * demand proxy — Annual occupation ceiling and actual grants over the
--                    most recent published programme year, sourced from
--                    Home Affairs SkillSelect (PY25/26 ceilings + PY24/25
--                    grants, joined at the 4-digit ANZSCO unit group level).
--
-- Demand proxy rationale
--   Home Affairs does not publish round-level EOI invitations broken down
--   per 6-digit ANZSCO. The annual ceiling (cap per unit group) plus actual
--   grants in the prior programme year are the most defensible public proxy
--   for competition intensity at the occupation level.
--
-- Derived ratios
--   jobs_to_ceiling_ratio = jobs_count_30d / annual_ceiling
--                              How many advertised jobs the market has per
--                              programme-year invitation slot. Higher is
--                              more favourable to applicants.
--   grants_to_jobs_ratio  = grants_py24_25 / jobs_count_30d
--                              How many actual grants were issued per
--                              currently advertised job. Higher means
--                              applicants face stiffer historical
--                              competition.
--
-- Jobs with no ANZSCO mapping are excluded from per-occupation aggregation.

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

ceilings as (

    select
        anzsco_unit_group,
        unit_group_name,
        tier,
        annual_ceiling,
        grants_py24_25,
        remaining_py25_26,
        program_year                                             as ceiling_program_year
    from {{ ref('occupation_ceilings') }}

)

select
    o.anzsco_code,
    o.occupation_name,
    o.is_mltssl,
    o.is_stsol,
    o.is_csol,
    o.list_membership,

    substr(o.anzsco_code, 1, 4)                  as anzsco_unit_group,
    c.unit_group_name,
    c.tier,

    coalesce(j.jobs_count_30d, 0)                as jobs_count_30d,
    coalesce(j.sponsorship_yes_count, 0)         as sponsorship_yes_count,
    coalesce(j.sponsorship_no_count, 0)          as sponsorship_no_count,
    coalesce(j.remote_count, 0)                  as remote_count,
    coalesce(j.hybrid_count, 0)                  as hybrid_count,
    coalesce(j.onsite_count, 0)                  as onsite_count,
    coalesce(j.local_experience_count, 0)        as local_experience_count,
    j.avg_salary,
    j.median_salary,

    c.annual_ceiling,
    c.grants_py24_25,
    c.remaining_py25_26,
    c.ceiling_program_year,

    safe_divide(
        coalesce(j.jobs_count_30d, 0),
        nullif(c.annual_ceiling, 0)
    )                                            as jobs_to_ceiling_ratio,

    safe_divide(
        c.grants_py24_25,
        nullif(coalesce(j.jobs_count_30d, 0), 0)
    )                                            as grants_to_jobs_ratio

from {{ ref('dim_occupation') }} o
left join job_aggregates j using (anzsco_code)
left join ceilings c
    on substr(o.anzsco_code, 1, 4) = c.anzsco_unit_group
