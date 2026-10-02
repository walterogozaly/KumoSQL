# SPES Calcite pairs not in SQLSolver's set

The SPES Calcite pairs whose text SQLSolver's Calcite fixture does not already
hold.

* Source: https://github.com/georgia-tech-db/spes, `testData/calcite_tests.json`
* Commit: `8049f98d8e64a5c2c43d1ed581e7d5b6e0a006e2`
* Licence: Apache-2.0 (see `LICENSE`)
* Extractor: `tools/spes_to_sql.py` (regenerate with
  `python tools/spes_to_sql.py --src <SPES checkout>`; needs `duckdb`)
* Schema: SQLSolver's Calcite schema, `tests/fixtures/sqlsolver/calcite.schema.sql`
* Test: `tests/test_cosette_fixtures.py`

SPES's 232 pairs are the same Calcite tests, in the same order, as
`tests/fixtures/sqlsolver/calcite_pairs.txt` (see
`tests/fixtures/cosette/README.md`), but SQLSolver edited some queries. A pair
is left out when SQLSolver holds it after whitespace, case and
trailing-semicolon normalisation (135) or after table aliases are renamed in
order of appearance (2). That leaves **95 SPES-only pairs**, each a different
text of a test SQLSolver has at the same position (`sqlsolver_index`).

Each is parsed with sqlglot (`read="mysql"`), transpiled to DuckDB and run on
an empty and 40 random databases of the schema. **34 are kept** in
`spes_only_pairs.jsonl` (`name`, `spes_index`, `label`, `sql_a`, `sql_b`,
`sqlsolver_index`); 61 are in `spes_only_skipped.jsonl` with the reason:

| skipped | category |
| ---: | --- |
| 31 | DuckDB cannot run it (SPES's SQL references aliases out of scope, nests aggregates, names `VALUES` columns `EXPR$0`, multi-argument `COUNT(DISTINCT a, b)`, casts or comparisons DuckDB rejects) |
| 22 | sqlglot cannot parse it (an empty `(VALUES)` relation) |
| 7 | the two sides differ on a random database |
| 1 | uses `||`, Calcite's string concatenation, which sqlglot's mysql dialect reads as `OR` |

Label: `equivalent` (each pair is a Calcite rule's input and output plan, as
SPES wrote them out), except three pairs labelled `not_equivalent` with a
`label_note`: `testSemiJoinRule`, `testSemiJoinRuleExists` and
`testSemiJoinTrim`. SPES's text replaces the semi-join with an inner join on
EMP rows that are not de-duplicated, so each DEPT row repeats once per
matching employee; nothing makes `EMP.DEPTNO` unique, so the texts differ
under bag semantics (Calcite's rule is fine; the SPES transcription is not).
`cosette_bench.py` refutes the first two with a DuckDB database.
