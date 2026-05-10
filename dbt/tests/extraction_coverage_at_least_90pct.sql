-- Singular test: assert the LLM extraction reaches at least 90 percent of
-- rows in int_jobs_enriched. The pipeline considers any miss outside this
-- band an operational regression worth surfacing in CI.

with totals as (

    select
        countif(has_extraction)                       as enriched_rows,
        count(*)                                      as total_rows,
        safe_divide(countif(has_extraction), count(*)) as coverage_ratio
    from {{ ref('int_jobs_enriched') }}

)

select *
from totals
where coverage_ratio is null
   or coverage_ratio < 0.90
