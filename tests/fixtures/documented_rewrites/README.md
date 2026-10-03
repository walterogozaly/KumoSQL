# Documented rewrites

Query pairs written for KumoSQL, each mirroring one before/after rewrite that documentation
recommends, scored by `tools/documented_rewrites_bench.py`.

* Sources: SQLFluff's rule reference, dbt's SQL style guide, BigQuery's performance best practices,
  Snowflake's QUALIFY reference and dynamic-table guide, AWS's Redshift query best practices and
  Microsoft's SQL Server high-CPU troubleshooting page. Each case links its page in `source`.
* The claims were collected by an outside research assistant (files R012 and R012b, 2026-10-03) and
  fact-checked; every label here was assigned again by hand and is checked on DuckDB by the tests.
* No documentation text is copied: the queries use their own tables and columns and are written in
  the BigQuery dialect, so they are KumoSQL's own cases (same licence as the repository). `claim`
  paraphrases what the page recommends.

## Files

`cases.jsonl`: `id` (the research file's id), `left`, `right`, `label`, `note`, `schema`, `dialect`,
`source`, `claim`, and for `not_equivalent` cases a `counterexample` (table -> rows).

| label | cases |
| --- | --- |
| equivalent | 19 (SQLFluff structure, convention and ambiguity rules, dbt style, BigQuery LIKE for a substring regex, Snowflake QUALIFY for a filtered ROW_NUMBER) |
| not_equivalent | R012b-09 (`= NULL` against `IS NULL`; the rule fixes a bug), R012b-23 (latest row per key by a MAX join against RANK: a NULL key), R012-037 (a cast-to-date filter against a range that spans a year), R012-039 (`IN (subquery)` against a join that repeats rows), R012-040 (a pushed filter on another column), R012-042 (a filter on a different column) |

Left out: the BigQuery legacy-SQL migration mappings (KumoSQL does not read legacy SQL), a pair
whose two queries are identical (R012b-15), APPROX_COUNT_DISTINCT and the two rewrites built on
nested arrays and APPROX_QUANTILES (R012b-18, 19, 21), ORDER BY .. LIMIT moved before DENSE_RANK
(R012b-20, equal only up to tie choice), and the two Databricks rewrites that need an unenforced key
(R012b-24, 25; conditional equivalence, scored elsewhere).

## Overlap with other evals

The SQLFluff rules here are also exercised by the SQLFluff fixtures eval
(`benchmarks/sqlfluff_rule_cases`), on SQLFluff's own test queries; these cases are separate text.
