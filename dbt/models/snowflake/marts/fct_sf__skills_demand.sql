{{
    config(
        materialized='table',
        cluster_by=['state'],
    )
}}

-- Skill mentions per state, joining the shredded postings to the extraction
-- results. This is the model the Snowflake slice exists to demonstrate: it is
-- the only one that needs LATERAL FLATTEN, and it reaches into an array that
-- was never declared as a column anywhere.
--
-- The BigQuery equivalent reaches the same answer with `unnest` over a
-- REPEATED column that had to be declared at load time, and Athena with
-- `cross join unnest ... as t(s)` over a column declared in Glue DDL. Three
-- idioms, three schema philosophies, one number — which
-- scripts/compare_engines.py checks rather than assumes.
--
-- Skills mentioned fewer than three times in a state are dropped: below that
-- the counts are extraction noise rather than signal.

with joined as (

    select
        j.state,
        j.source_city,
        e.required_skills
    from {{ ref('stg_sf__adzuna_jobs') }} j
    inner join {{ ref('stg_sf__llm_extract') }} e
        on e.job_id = j.job_id
       and e.snapshot_date = j.snapshot_date
    where j.state is not null

),

flattened as (

    select
        state,
        source_city,
        lower(trim(f.value::string))    as skill_normalised,
        f.value::string                 as skill_original
    from joined,
         lateral flatten(input => required_skills) f
    where length(trim(f.value::string)) > 0

)

select
    state,
    skill_normalised                    as skill,
    any_value(skill_original)           as skill_display,
    count(*)                            as mention_count,
    count(distinct source_city)         as cities_mentioned_in
from flattened
group by state, skill_normalised
having count(*) >= 3
