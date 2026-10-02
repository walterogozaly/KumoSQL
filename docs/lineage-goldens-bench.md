# Lineage goldens: DataHub and OpenLineage

Two public test suites state, for a SQL statement, the table and column lineage it must produce. This scores KumoSQL
against them (`python tools/lineage_goldens_bench.py [--details] [--write-results]`). They are scored separately and never
added together, because only one of them is an independent oracle.

| Corpus | Pin | Oracle |
| --- | --- | --- |
| [OpenLineage](https://github.com/OpenLineage/OpenLineage) `integration/sql/impl/tests` (Apache-2.0) | `7a84fd4d48aeed4d0e6f391e2b7fbc11c0ff4fd9` | a Rust parser (sqlparser-rs) that shares no code with sqlglot: **independent** |
| [DataHub](https://github.com/datahub-project/datahub) `metadata-ingestion/tests/unit/sql_parsing` (Apache-2.0) | `8a117ba1f5331b4133976fae4d5a99e20c3b67da` | goldens are sqlglot's own output, reviewed by maintainers: **not independent**, read as regression coverage |

`tools/lineage_goldens_harvest.py` rebuilds `tests/fixtures/lineage_goldens/{datahub,openlineage}.json` from clones of the
two repositories at those commits (the commands are in its docstring). Nothing about an expectation is changed.

## What was harvested

- DataHub: all 101 `assert_sql_result` calls in `test_sqlglot_lineage.py`; 99 harvested (2 reference golden files that do
  not exist upstream). 3 of them are skipped upstream (`pytest.mark.skip`) and are left out of the score.
- OpenLineage: all 158 `#[test]` functions under `integration/sql/impl/tests`; 140 harvested. 18 are not (a SQL string that
  is built, Snowflake stage and `IDENTIFIER()` syntax, an error-message assertion). A test marked `#[ignore]` upstream is
  left out of the score. Some table tests assert only the inputs or only the outputs; only that side is checked.

Original and adapted cases are kept apart: the fixtures hold the upstream SQL and expectation unchanged. The adaptation is
only in how a case is run and compared:

- one case is one single-model KumoSQL pipeline named after the `INSERT`/`CREATE ... AS` target, or `__select__`;
- DataHub schemas are handed over under the table spelling the query uses (DataHub keys them by full name and resolves
  unqualified names against `default_db`/`default_schema`);
- DataHub names a date shard or wildcard `table_yyyymmdd` and drops a partition decorator (`$...`): applied to both sides;
- OpenLineage calls an unnamed output `_0`; KumoSQL `_col_N`; both compare as "an expression";
- when a case supplies a default db or schema, table names match on the shorter name's dotted suffix.

## Outcomes

`exact`, `coarse` (no extra edge; a struct sub-field edge `a.b` is met only by an edge to its root column `a`), `unknown`
(KumoSQL said it could not trace something and claimed nothing wrong), `missed` (confident, but something expected is
absent), `wrong` (a table or edge is claimed that is not expected) and `disputed` (the oracle defines the answer differently;
each has one written reason in `DISPUTED`, and a test fails if one is used on a case that is not actually a mismatch).

Scope `in`: BigQuery (DataHub `dialect=bigquery`; OpenLineage `postgres`/`generic`/`bigquery`, which are plain ANSI here)
that parses as BigQuery and is not skipped upstream. Cases that are not BigQuery (`USE` state across statements, T-SQL
`SELECT INTO`, a table name hidden by its alias, Snowflake load statements) are listed in the tool output as left out.

## Baseline and what the first run found

First run, before any adapter fix: DataHub 7/18 exact with 3 wrong, OpenLineage 70/96 exact with 7 wrong, no product change
yet. Every one traced to the harness, not to KumoSQL: schemas keyed under two spellings made sqlglot report an ambiguous
table (DataHub), the DataHub shard and partition naming, OpenLineage's `_0` naming, two cases that are not BigQuery, and four
tests that treat an unused CTE as reading nothing. After those the in-scope numbers are in the README scoreboard rows.

The four `disputed` cases: `WITH unused AS (SELECT * FROM users) SELECT ... FROM other`. OpenLineage reports data flow, so
`users` is not an input. KumoSQL lists every table the statement names, because dropping `users` still breaks the query.

## Held out and limits

The other-dialect cases (109: Snowflake, MySQL, T-SQL and so on, read as BigQuery) were never adjudicated or used to
shape the adapter, so they are an unseen generalisation check; their wrong and missed counts are dialect differences and stay out of
the headline. The in-scope cases are not held out: each mismatch was read while building the adapter.

Table reads (not columns) are traced for `DELETE ... USING`, `UPDATE ... FROM`, `INSERT ... VALUES` with a subquery and
`CREATE TABLE ... LIKE/CLONE`, and for subqueries in script `SET`, `DECLARE ... DEFAULT` and `ASSERT`; the table such a statement writes is not a read, and ALTER/DROP/TRUNCATE are scored by the
table they write. These models stay column-blind (`unknown_reads`). Still `unknown`, never guessed: `MERGE` and multi-statement
scripts (other work), the no-`FROM` BigQuery `DELETE` form, the second table of a multi-table `DROP`, and a partition-decorated
table name. Struct
sub-field lineage is coarse (the root column).
