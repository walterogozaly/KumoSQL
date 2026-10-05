# QED Calcite equivalence pairs

SQL pairs converted from the Calcite test corpus of the QED prover.

* Source: https://github.com/qed-solver/prover, `tests/calcite/*.json`
* Commit: `9e9c2621d6d922007694a72f9cc2d5ed0de2eccd`
* Licence: MIT, (c) 2021 The QED Team (see `LICENSE`)
* Converter: `tools/qed_to_sql.py` (regenerate with
  `python tools/qed_to_sql.py --src <prover>/tests/calcite`)
* Checker: `python tools/validate_qed_pairs.py` (needs `duckdb`)

## Files

| file | content |
| --- | --- |
| `qed_calcite_pairs.jsonl` | one case per line: `name`, `schema_id`, `sql_a`, `sql_b`, `ddl` (CREATE TABLE text), `schemas` (structured: table, columns with type/nullable, keys) |
| `qed_calcite_schemas.json` | each distinct DDL set by `schema_id` |
| `qed_calcite_skipped.jsonl` | skipped cases with the reason |
| `summary.json` | counts |

Every QED test is a Calcite rewrite rule, so the two sides of a pair are meant
to be equivalent (QED proves them); the corpus has no deliberately
inequivalent pairs.

## Result

444 files: **390 converted**, 54 skipped.

| skipped | reason |
| ---: | --- |
| 16 | aggregate FILTER and grouping sets (neither is represented in QED's IR) |
| 7 | aggregate FILTER clause (not represented in QED's IR) |
| 7 | unsupported operator ST_POINT |
| 5 | unsupported aggregate LITERAL_AGG |
| 4 | integer-typed STDDEV/VAR aggregate (Calcite integer arithmetic not modelled) |
| 4 | unsupported aggregate SINGLE_VALUE |
| 2 | table is empty only by test-harness convention (not expressible in the schema) |
| 2 | nondeterministic aggregate ANY_VALUE |
| 2 | aggregate WITHIN DISTINCT (not represented in QED's IR) |
| 1 | unsupported aggregate GROUPING |
| 1 | implicit cross-type comparison |
| 1 | column type ANY |
| 1 | unsupported operator USER |
| 1 | LIMIT/OFFSET without ORDER BY (nondeterministic) |
(The first matching reason is recorded, so a case that has several is counted once.)

## Conversion notes

* MySQL-flavoured SQL, parseable by sqlglot (`read="mysql"`). Tables are named by
  the last component of the schema name; scans select the schema's own field
  names and alias them `c0..cN`. Every other operator is a derived table whose
  columns are `c0..cN`, so QED's ordinal column references become `alias.c<i>`.
  Calcite flattens struct columns into names like `"F1"."A0"`; the quotes are
  dropped and the name is back-quoted.
* Subqueries (EXISTS, IN, scalar, ANY/ALL) and the right side of `correlate`
  follow QED's convention: ordinals count the enclosing relation's columns first,
  then the subquery's own input. These become correlated references
  (`LATERAL` for INNER/LEFT correlate, [NOT] EXISTS for SEMI/ANTI).
* `union` is UNION ALL, `distinct` is SELECT DISTINCT, `intersect` and `except`
  are the set forms (the Calcite plans say `all=[false]`). SEMI/ANTI joins are
  [NOT] EXISTS. Multi-column COUNT(DISTINCT ...) and modeled window functions
  with supported OVER clauses are emitted directly.
* `CURRENT_TIMESTAMP` is emitted as a dynamic SQL expression. A zero-column
  projection is represented only when its parent observes row cardinality, and
  Calcite ROW fields are flattened to their component columns.
* `SEARCH`/Sarg is expanded to comparisons; `IS NOT DISTINCT FROM` is `<=>`;
  `||` is `CONCAT`; integer `/` is `DIV`; AVG over an integer type is
  `SUM DIV COUNT` (Calcite types it as an integer); `+ - *` on integers cast
  operands to the result type (Calcite promotes, DuckDB would overflow).
* Sort: QED's collation has no NULLS FIRST/LAST, so plain `ORDER BY` is emitted
  (MySQL: NULLs lowest). DECIMAL columns are `DECIMAL(19, 2)` and VARCHAR is
  `VARCHAR(255)` because the IR carries no precision or length.
* Keys: the first key whose columns are all NOT NULL is `PRIMARY KEY`, the rest
  are `UNIQUE`.
* Skipped, never guessed: aggregate FILTER / WITHIN DISTINCT (the IR drops them;
  detected from the `help` plan text), GROUPING, LITERAL_AGG, SINGLE_VALUE,
  ANY_VALUE, geospatial functions, struct (`ANY`) columns, implicit cross-type
  comparisons, tables that are empty only by Calcite test-harness convention,
  LIMIT without ORDER BY, and anything else the converter does not model exactly.
  Grouping sets are skipped when their plan also has a FILTER clause that QED's
  IR omits.

## Validation

`tools/validate_qed_pairs.py` creates each case's schema in DuckDB (sqlglot
transpiles MySQL to DuckDB), fills it with random small databases that respect
NOT NULL and keys (NULLs sort first), runs both sides and compares the result
multisets. The proof benchmark also executes each pair on random DuckDB
databases; it reports 371 proved, 19 unknown, 0 different, and 0 wrong across
the 390 converted cases. This checks conversion consistency, not equivalence.
