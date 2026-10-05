# MATCH_RECOGNIZE

[Plain-language version](../docs_simple/match-recognize.md)

BigQuery's `MATCH_RECOGNIZE` clause looks for a pattern in the rows of a table or subquery (`PARTITION BY`, `ORDER BY`, `MEASURES`, `PATTERN`, `DEFINE`). sqlglot's BigQuery tokenizer has no keyword for it, so every spelling used to stop at `Expecting )`. KumoSQL reads it inside `bigquery_syntax.py` and `match_recognize.py`, the same way on pure and compiled sqlglot and on 26.0.0, 30.20 and 30.21, traces the columns it returns, and keeps every prover and cleanup rule away from it.

```sql
SELECT m.p, m.fv
FROM d.events MATCH_RECOGNIZE (
  PARTITION BY p ORDER BY ts
  MEASURES FIRST(v) AS fv, MATCH_NUMBER() AS mn
  AFTER MATCH SKIP TO NEXT ROW
  PATTERN (^ X+ Y? | Z{2,3} $)
  DEFINE X AS v > PREV(v), Y AS v < 5, Z AS v = 1
  OPTIONS (use_longest_match = TRUE)
) AS m
```

## What is read

The grammar is BigQuery's, which is narrower than the standard's (every spelling here was checked against BigQuery with a dry run, and `tests/fixtures/bq_syntax` keeps the valid ones):

```
operand MATCH_RECOGNIZE ( [PARTITION BY ...] ORDER BY ... MEASURES expr AS name, ...
                          [AFTER MATCH SKIP PAST LAST ROW | TO NEXT ROW]
                          PATTERN (...) DEFINE name AS condition, ... [OPTIONS (...)] ) [[AS] alias]
```

- The operand is a table (a path, quoted or dashed, with an alias), a subquery or a `WITH` table. A pipe `|> MATCH_RECOGNIZE (...)` is the same operator applied to the query before it. The clause may stand first or last in a `FROM`/`JOIN` list, with a `WHERE`, `ORDER BY`, `LIMIT` or set operation after the select, inside a `WITH` table or a subquery, and in a script.
- The clause is read as `exp.MatchRecognize` in the `match` argument of a select. Where more than one relation is in reach sqlglot cannot say which one the clause applies to (it reads it as belonging to the whole `FROM`), so the text is rewritten before it is parsed: `src MATCH_RECOGNIZE (...) AS m` becomes the derived table `(SELECT * FROM src MATCH_RECOGNIZE (...)) AS m`. The printer turns that wrapper back into the text that was written, so a statement prints as it was read.
- `OPTIONS (...)` after the last `DEFINE` condition, which sqlglot has no place for, is held in that condition as a marker call that prints back as written.
- The result keeps the pattern text exactly (quantifiers, `{n,m}`, reluctant `?`, alternation, anchors, grouping).

## What is refused

A construct BigQuery rejects is a `ParseError`, so a prover is never handed SQL BigQuery would not run (each was dry-run):

| Refused | BigQuery says |
| --- | --- |
| `ONE ROW PER MATCH`, `ALL ROWS PER MATCH`, `FINAL`, `RUNNING` | syntax error: BigQuery has no rows-per-match, final or running |
| `AFTER MATCH SKIP TO FIRST/LAST var` | only `PAST LAST ROW` and `TO NEXT ROW` exist |
| no `ORDER BY`, `MEASURES`, `PATTERN` or `DEFINE`; clauses out of order or repeated | syntax error |
| a `MEASURES` item without `AS name` | syntax error |
| a clause inside the table or the body of another, or two in a row in a pipe | "Nested MATCH_RECOGNIZE is not allowed" (reading it through a `WITH` table is allowed and read) |
| after `UNNEST(...)`, a join, a `FOR SYSTEM_TIME` or anything but a table or subquery | "not allowed with array scans", syntax error |
| followed by `TABLESAMPLE`, `PIVOT` or `UNPIVOT` | "Unsupported combination of table operators" |

## What it returns, and lineage

The result is the partition columns followed by the `MEASURES`, and nothing else: a `SELECT *` over the clause does not return the table's columns (checked by dry run). sqlglot's optimizer expands that star to the operand's columns, which would put columns that are not in the result into every analysis and trace the ones that are (`fv`) to nothing.

`match_recognize_view.lineage_form` writes the same select as plain SQL for tracing only: `SELECT p, FIRST(v) AS fv FROM operand WHERE <DEFINE conditions> AND <ORDER BY keys> IS NOT NULL GROUP BY p`. It is never printed, proven or run. Pipeline analysis (`pipeline.py`) and the schema-change check (`schema_change.py`) use it, so:

- the tables the operand reads are read, and so is every column the clause names: partition, `ORDER BY`, `MEASURES` and `DEFINE` (a column used only in `DEFINE` is read and counts as deciding which rows come back);
- the output columns are named as BigQuery names them: a partition column keeps its name, any other partition expression is `f0_`, `f1_`, a name that stands again becomes `name_1`, `name_2` (dry run: `PARTITION BY p + 1, p MEASURES COUNT(*) AS p` returns `f0_, p, p_1`);
- a measure traces to the columns it reads (`FIRST(v)` to `v`); `high.v` inside a measure is `v`, because the pattern variable is not a table; `COUNT(*)` and `MATCH_NUMBER()` read no column, as a constant aggregate does elsewhere;
- when the names cannot be told (a repeated name that is also written as `name_1`, a clause that is not the whole of its select) the model's columns are unknown and its readers are untraced, never traced to the wrong table.

## Provers, cleanup and rewrites

- Every prover declines. The SMT prover says `unsupported: MATCH clause`; the structural prover compares the whole clause text, so different `DEFINE`, `PATTERN`, `AFTER MATCH SKIP`, `ORDER BY`, `MEASURES` or `OPTIONS` are never equal; `parse_check.guarded` refuses a proof of any text with the clause (its independent reader does not know the construct, so sqlglot's reading of it cannot be checked); `check_containment`, `check_bounded` and the model-reuse proposer return `unsupported`/`unknown`.
- `output_properties.infer_properties` reports the query as unsupported instead of the table's columns.
- A statement with the clause is left as written by every cleanup rule (`match_recognize_kept`), because a rule that rebuilds the select around it, lifts the table it reads into a `WITH` or inlines a `WITH` table could change what rows match. Other statements of the same script are still rewritten. `tidy` and `simpler_forms` return the query as written. `format_sql` cannot parse the clause (sqlfluff) and leaves the text unchanged.
- The DuckDB translation refuses the clause (DuckDB has none), so no local check runs it.

## Limits

- Only what BigQuery accepts is read. A construct BigQuery adds later is refused until it is checked.
- Lineage is column-level; it does not say which rows match.
- A retyped input column is reported as unaffecting a model whose measures have no known result type (`FIRST(v)`), as for any function sqlglot does not type.
- Equivalence of two `MATCH_RECOGNIZE` queries is never proven, not even of a query with itself.

Tests: `tests/test_match_recognize.py`. The syntax fixtures are in [bigquery-syntax-coverage.md](evals/bigquery-syntax-coverage.md).
