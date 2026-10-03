# Lineage and change-impact evals

[Plain-language version](../../docs_simple/evals/lineage-bench.md)

Two evals score the graph, changes and cost pages' foundation: table and column lineage, and "what breaks if I change this". Neither runs a language model. Scores keep four things apart: **correctness** (nothing wrong, nothing unsafe), **analysis quality** (precision and recall), **coverage** (how much is traced rather than reported unknown) and **performance** (time and memory by pipeline size). Unknown beats wrong: a column KumoSQL cannot trace is reported `unknown`, never guessed.

The scoreboard rows live in [benchmarks/results/](../../benchmarks/results) (`sqllineage.json`, `lineage-impact.json`) and are rendered into the README by `python tools/scoreboard.py`. The floors behind them are in `tests/test_lineage_benchmarks.py`.

## SQLLineage test cases

[SQLLineage](https://github.com/reata/sqllineage) (MIT, Copyright (c) 2019 Reata) has about 540 test functions, each a SQL statement with the table and column lineage it must produce. `tools/sqllineage_harvest.py` runs those tests unmodified with their assertion helpers replaced by recorders, and writes the pairs to `tests/fixtures/sqllineage/cases.json` (532 cases after removing variants that repeat a case without its schema). The expectations are SQLLineage's, not ours.

`python tools/sqllineage_bench.py [--details] [--write-results]` turns each case into a one-model pipeline (named after the `INSERT` / `CREATE TABLE AS` target, with the case's schemas as `source_schema`) and compares KumoSQL's table dependencies (`Pipeline.table_reads()`) and column lineage (`explain_lineage()`) with the expected graph. A case ends in one of:

- `exact`: every expected edge and table, nothing extra
- `unknown`: KumoSQL said it could not trace something (a `SELECT *` over a table with no known columns, `UPDATE`, procedures, a script that creates a view and reads it) and claimed no wrong edge. An edge from `table.*` counts as honest: it says "some column of this table"
- `missed`: confident, but an expected edge is missing
- `wrong`: confident, and an edge or table that is not expected is claimed

Cases that are not about BigQuery lineage are left out and listed with the reason: other dialects (read as BigQuery, which is what the tool does with every project; reported apart so they never move the headline), lateral column alias references, SQL BigQuery does not accept (bare `UNION`, double-quoted identifiers, `::` casts), `INSERT` columns mapped by the target table's column order, a `LEFT JOIN ... USING` key counted from both sides, and `DROP`/`RENAME` table lifecycles.

**Score: 254/278 exact, 0 wrong, 24 unknown, 0 missed** (table cases 101/108, column cases 153/170). Column edge precision 0.993 and recall 0.864 (recall counts what is reported unknown). The first run was 211/282 exact with 21 wrong; MERGE support (all of SQLLineage's MERGE cases, table and column) moved it from 235/279. The harness names a `MERGE` target as the case's destination, as it does for `INSERT` and `CREATE ... AS`.

SQLLineage assumes that `INSERT INTO t SELECT ...` writes columns named like the SELECT's outputs. BigQuery maps them to `t`'s own columns by position, so without `t`'s schema KumoSQL reports the outputs unknown (`insert_target_columns`) rather than naming them. When a case gives no schema for `t`, the harness states SQLLineage's assumption as `t`'s schema (the SELECT's output names, in order); it does not when the `INSERT` reads `t` itself. Fixing the guess (2026-10-03) moved the score from 257/279: two cases are now unknown (an `INSERT` reading its own target, and one selecting the same column twice under names that differ only in case), and one case is left out because SQLLineage counts both sides of a `LEFT JOIN ... USING` key, while BigQuery takes the left input's value.

This is a floor, not a held-out score: bugs it found were fixed in the same change. What it found:

- `WITH c AS (...) INSERT INTO t SELECT ...` dropped the CTEs, so lineage pointed at the CTE name instead of its source tables.
- `INSERT INTO t (a, b) SELECT ...` and `CREATE VIEW v (a, b) AS ...` named outputs by the SELECT's aliases instead of by position.
- An unaliased `CAST(a AS T)` was named `a`; BigQuery names it `f0_`, so it now gets a generated name that cannot be mistaken for the column.
- Statements of a script other than the last were ignored: now the tables they read are dependencies, and DML or raw-text statements that might read tables are counted in the `skipped_statements` diagnostic instead of vanishing.

Known gaps (reported as unknown, not guessed): `UPDATE`/procedures, views and temporary tables created earlier in the same script, `INSERT` without a column list into a table whose schema is not known.

## Lineage and change impact

`python tools/lineage_bench.py [--scale] [--write-results]` generates pipelines in Python where the SQL and the answer come from the same small spec, so no parser produces the answer key. For each model the spec records:

- `edges`: output column to the source columns its value is computed from
- `consumed`: every source column the model names anywhere (select list, filter, join, grouping, window, a CTE nothing uses, `EXCEPT` lists)
- `parents`: the tables it reads

Impact truth follows the tool's documented rule: dropping a column breaks every model that reads it, and everything downstream of those models is `indirect`. Dead-column truth: a column is dead only if no model reads it.

Families, from a handful of models to thousands (`--scale` runs 100, 1,000 and 3,000, each in its own process):

| Group | Families |
| --- | --- |
| dev | passthrough and rename with aliases and qualified or bare names, columns used only in a filter, expressions, aggregates with `HAVING`, joins (columns used only in `ON`), CTE chains, `SELECT *` over known schemas, sources with no declared schema, `UNION ALL [FULL / LEFT / INNER] BY NAME` and `CORRESPONDING` (added after a user report: before the fix 200 of 377 of its columns traced to the wrong sources) |
| held-out | `UNION ALL`, window functions, `IN (SELECT ...)`, `SELECT * EXCEPT ... REPLACE ...`, `SELECT *` inside a CTE, nested subqueries |
| special | unparseable SQL and `SELECT *` over undeclared columns (must be unknown), dependency cycles (must be flagged, must not crash) |

Measured, and reported separately:

| | Score |
| --- | --- |
| Correctness | 2,737/2,737 columns traced to exactly the right sources, 0 wrong; 696/696 drop-column answers exact, 0 impacted models missed; 0 columns called dead that are read; 18/18 cycles flagged; 0 unreadable models traced anyway |
| Analysis quality | edges precision/recall 1.000/1.000; reads 1.000/1.000; table dependencies 1.000/1.000; impacted models precision 1.000, recall 0.937 (the rest are listed as unknown readers); dead columns found 26/83 on dev (it declines to call a column dead when any reader is unknown) |
| Coverage | 100% of readable columns traced |
| Performance | 100 models 0.34 s, 64 MB peak; 1,000 models 3.2 s, 101 MB; 3,000 models 9.7 s, 159 MB (peak resident memory including the generator) |

**Held-out families, first run** (474 models, 1,151 columns, before anything they found was fixed): 0 columns traced wrongly, but 83 impacted models missed and 13 live columns called dead, all from `SELECT * EXCEPT (...)`: an excepted column is named by the query but vanishes when the star is expanded, so it was not counted as read. After the fix these families no longer count as held out; `tests/test_lineage_benchmarks.py` also runs seeds the results file does not use.

Bugs the suite found, all fixed with regression tests in `tests/test_lineage_regressions.py`:

1. A column named in a CTE or subquery select list was not counted as read when nothing used the CTE's column, so `assess_change` said dropping it affects nothing and `dead_columns()` could call it dead. The query would fail in BigQuery. Columns named in the SQL now count; a `*` expanded inside a CTE still reads only what the outer query uses.
2. Columns named in `SELECT * EXCEPT (...)` were not counted as read (above).
3. In a dependency cycle, columns read by the other cycle member were reported dead and the member was missing from the impact result. Cycle members are now unknown readers and never make a column dead.

## Credit

SQLLineage's test cases are used under its MIT licence; the licence and copyright notice are recorded in `tests/fixtures/sqllineage/cases.json` (`source`) and in `tools/sqllineage_harvest.py`.

## Set operations that match columns by name

`UNION [ALL|DISTINCT] BY NAME`, `FULL` / `LEFT` / `INNER ... [OUTER]` and `CORRESPONDING` pair columns by name, while sqlglot's lineage pairs them by position. `kumosql.set_operations.positionalize` rewrites them to the positional form (each branch projected to the output columns in order, `NULL AS col` for a column a branch lacks) before tracing, pruning or proving, so it does not depend on the sqlglot version. A branch with an unexpanded star or duplicate names, or a plain `BY NAME` whose branches have different columns (an error in BigQuery), is reported as unknown (`by_name_set_operation`) rather than traced. The provers and the output-property and table-profile analyses do the same: they prove only after positionalizing, and otherwise return not proven. Tests: `tests/test_set_by_name.py` and the `union_by_name` family above.
