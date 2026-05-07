# dbt seeds

Sample reference data for the raw layer. Each CSV is loaded into the
``raw`` BigQuery dataset by ``dbt seed``.

| File | Purpose | Production source |
|------|---------|-------------------|
| ``occupation_lists.csv`` | MLTSSL / STSOL / CSOL occupation membership. | https://immi.homeaffairs.gov.au/visas/working-in-australia/skill-occupation-list |
| ``eoi_invitations.csv`` | EOI invitation round outcomes by visa subclass. | https://immi.homeaffairs.gov.au/what-we-do/skillselect/key-facts |

The committed CSVs are intentionally small and illustrative. Replace them
with full extracts before running production analysis. Column names and
types are pinned in ``dbt/dbt_project.yml`` under ``seeds:``.
