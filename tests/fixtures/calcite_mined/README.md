# Calcite optimizer tests mined into SQL pairs

SQL equivalence pairs mined from Apache Calcite's current rule tests, beyond the
frozen paper collections (SQLSolver/SPES/Cosette, QED, R-Bot).

* Source: https://github.com/apache/calcite, commit
  `2d28ce3244dd9799909c44b1fa8a2fb4e4eda673` (2026-09-30). Files:
  `core/src/test/resources/org/apache/calcite/test/RelOptRulesTest.xml` plus the
  per-rule `AggregateFilterToFilteredAggregateRuleTest`,
  `AggregateReduceFunctionsOnGroupKeysRuleTest`, `AggregateRemoveDuplicateKeysRuleTest`,
  `CombineRelOptRulesTest`, `JoinAggregateTransposeRuleTest` and
  `OuterJoinToAntiJoinRuleTest` XML files. Table definitions come from
  `testkit/.../catalog/MockCatalogReaderSimple.java` (SALES/CUSTOMER schemas) and,
  for RelBuilder-built tests over the `scott` schema, from
  [scott-data-hsqldb](https://github.com/julianhyde/scott-data-hsqldb)'s `scott.script`
  (commit `9740957d32dfa86410c1039e0cb027a34ed6d69a`).
* Licence: Apache-2.0, (c) The Apache Software Foundation (see `LICENSE` and `NOTICE`).
* Converter: `tools/calcite_plan_to_sql.py` (regenerate with
  `python tools/calcite_plan_to_sql.py --calcite <calcite checkout> --sqlsolver-names <spes>/testData/calcite_tests.json`,
  then `python tools/validate_calcite_mined_pairs.py --write`). A sparse checkout is
  enough: `core/src/test/resources/org/apache/calcite/test` and
  `core/src/test/java/org/apache/calcite/test` (the Java is read to skip tests that use
  a custom catalog, dynamic table or type system).
* Checker: `tools/validate_calcite_mined_pairs.py` (needs `duckdb`).

## Files

| file | content |
| --- | --- |
| `pairs.jsonl` | one translated test per line: `name`, `source` (XML file), `calcite_commit`, `schema_id`, `sql_a` (planBefore), `sql_b` (planAfter), `sql_calcite` (the test's original SQL in Calcite's dialect, `null` for RelBuilder tests; reference only), `in_sqlsolver`, `in_qed`, `in_rbot`, `new`, `differs_in_duckdb`, `duckdb_status` |
| `skipped.jsonl` | every other test with its `reason` and the same provenance flags; "unchanged" tests (no planAfter, planAfter identical to planBefore, or plans that differ only in names and annotations) are recorded here with an `unchanged (...)` reason |
| `schemas.json` | `schema_id` -> `{ddl, tables}`: CREATE TABLE text (MySQL) and the structured columns/types/nullability/keys |
| `duckdb_counterexamples.jsonl` | for each pair whose two sides disagree in DuckDB: the database and both results |
| `summary.json` | counts: tests, translated, unchanged, skipped by reason, new vs already covered, DuckDB validation |

Names are Calcite test-method names; tests from the smaller XML files are prefixed
with their class (`OuterJoinToAntiJoinRuleTest.testX`). Provenance compares the
bare RelOptRulesTest name with SQLSolver's 232 Calcite pairs (names from SPES's
`testData/calcite_tests.json`, same order), QED's converted and skipped cases
(`tests/fixtures/qed/`) and R-Bot's `rewrites[].name` (`tests/fixtures/rbot/calcite.jsonl`).
`new` means the test is in none of them; tests from the smaller XML files are always new.

## Result

1016 tests: **502 translated** (168 new: 142 from RelOptRulesTest, 26 from the other
files), 182 unchanged, 332 skipped.

| translated pairs already covered by | count |
| --- | ---: |
| SQLSolver | 168 |
| QED | 332 |
| R-Bot | 35 |

DuckDB (8 random databases per pair, NOT NULL and keys respected): 500 agree, 1
differs, 1 cannot run in DuckDB (`testExpandJoinExists`: DuckDB cannot plan a
subquery in an outer-join condition).

* `testFullJoinToLeftAndRightJoin` (new) **differs**: Calcite rewrites
  `FULL JOIN ON e1.sal = e2.sal AND e1.mgr IS NULL` into
  `LEFT JOIN ... UNION ALL (RIGHT JOIN ... WHERE e1.sal <> e2.sal OR e1.mgr IS NOT NULL)`,
  which drops right rows with no partner (their left columns are NULL, so the filter is
  UNKNOWN). The translation is exact; this is a genuine non-equivalence in Calcite's
  expected plan, kept and flagged `differs_in_duckdb` with the counterexample saved.

Most frequent skip reasons (full list in `summary.json`): grouping sets 33, window
functions 23 + `LogicalWindow` 8, Calcite-internal operators (`HyperGraph` 13,
`Combine` 13, `MultiJoin` 8, `Uncollect` 7, `Sample` 5, `Enumerable*` 14), empty
`LogicalValues` under a join or correlate 18 (the digest omits its row type), aggregate
`WITHIN DISTINCT` 11, LIMIT/OFFSET that is not a literal 10 or has no ORDER BY 10,
statistical aggregates (`STDDEV_*`, `VAR_*`) 15, `SINGLE_VALUE`/`ANY_VALUE` 12, tables outside the modelled
catalogs (GEO, STRUCT, nested, custom) 17, `CURRENT_TIMESTAMP`/`RAND`/`USER` 10.

## Conversion notes

* Each pair is (planBefore, planAfter), the exact equivalence the test asserts. The
  original SQL is not used as `sql_a` because it is in Calcite's dialect (integer `/`,
  implicit coercions, CHAR padding, Calcite-only functions) and 86 tests have no SQL at
  all. As a check of the planBefore translation, 460 of the 465 pairs whose original SQL
  runs in DuckDB (read as PostgreSQL) agree with it; the 5 others differ only because of
  Calcite typing (integer `AVG`) or DuckDB's NULL handling in row `IN` lists.
* MySQL-flavoured SQL, parseable by sqlglot (`read="mysql"`), with the conventions of
  `tools/qed_to_sql.py`: scans alias the table's columns `c0..cN`, every operator is a
  derived table with columns `c0..cN`, Calcite's `$i` becomes `alias.c<i>`.
  Correlation variables (`$cor0.DEPTNO`) are resolved by field name (Calcite's join
  field uniquification, `DEPTNO0`, is reproduced) and become outer references;
  `LogicalCorrelate` becomes `CROSS JOIN LATERAL` / `LEFT JOIN LATERAL ... ON TRUE` /
  `[NOT] EXISTS`.
* Types are inferred bottom-up as Calcite does (INTEGER arithmetic stays INTEGER, so
  integer `/` and `AVG` become `DIV` and `SUM(x) DIV COUNT(x)`; COUNT is BIGINT). Anything
  whose result depends on Calcite typing that the SQL cannot state is skipped: DECIMAL
  division and AVG, casts that round or truncate, CHAR values of different lengths
  combined in CASE/UNION/VALUES (Calcite pads them), padded literals.
* `SEARCH(x, Sarg[...])` expands to comparisons, including `NULL AS TRUE/FALSE`.
  Aggregate `FILTER $f` becomes `agg(CASE WHEN f THEN x END)` (`COUNT()` -> `COUNT(CASE
  WHEN f THEN 1 END)`), `$SUM0` is `COALESCE(SUM(x), 0)`, `LITERAL_AGG(lit)` with group keys
  is the literal.
* `ORDER BY` keys carry an explicit null-ordering key, `(x IS NULL)` / `(x IS NULL) DESC`,
  because MySQL has no `NULLS FIRST/LAST`; Calcite's default is ASC NULLS LAST, DESC
  NULLS FIRST. `OFFSET` without a fetch is `LIMIT 9223372036854775807 OFFSET n`.
* An empty `LogicalValues` (`tuples=[[]]`) does not print its row type. At the plan root
  it takes the other plan's root types; under Project/Filter/Sort/Aggregate it is an
  empty derived table wide enough for the columns referenced; under a join, correlate or
  set operation the test is skipped.
* DDL keeps Calcite's types (`VARCHAR(20)`, `TIMESTAMP`, scott's `DECIMAL(7, 2)`,
  `TINYINT`) and declares the catalog's keys (`PRIMARY KEY`, or `UNIQUE` on a nullable
  key such as `DEPTNULLABLES.DEPTNO`).
