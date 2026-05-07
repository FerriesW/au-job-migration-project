{#
    Override of dbt's default `generate_schema_name` macro.

    Default behaviour concatenates `target.schema` with the model-level
    `+schema:` config to produce names like `staging_raw`, which is useful
    for multi-developer sandboxes but not for this single-tenant project.

    Behaviour after override:
      - If a model declares `+schema: <name>`, write to `<name>` exactly.
      - Otherwise, fall back to the profile's `target.schema`.

    Reference: https://docs.getdbt.com/docs/build/custom-schemas
#}

{% macro generate_schema_name(custom_schema_name, node) -%}
    {%- if custom_schema_name is none -%}
        {{ target.schema | trim }}
    {%- else -%}
        {{ custom_schema_name | trim }}
    {%- endif -%}
{%- endmacro %}
