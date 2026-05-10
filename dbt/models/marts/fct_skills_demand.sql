{{
    config(
        materialized='table',
        cluster_by=['state'],
    )
}}

-- Q3 supporting fact for the dashboard skills view. Unnests the LLM
-- required_skills array and aggregates per (state, skill). Emits only
-- skills mentioned at least three times in a given state to suppress
-- one-off noise.

with skills_unnested as (

    select
        state,
        source_city,
        lower(trim(skill))                                       as skill_normalised,
        skill                                                    as skill_original
    from {{ ref('int_jobs_anzsco_mapped') }},
    unnest(required_skills) as skill
    where state is not null
      and skill is not null
      and length(trim(skill)) > 0

)

select
    state,
    skill_normalised                                             as skill,
    any_value(skill_original)                                    as skill_display,
    count(*)                                                     as mention_count,
    count(distinct source_city)                                  as cities_mentioned_in
from skills_unnested
group by state, skill_normalised
having mention_count >= 3
