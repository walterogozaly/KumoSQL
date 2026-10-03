# Cosette equivalence examples

SQL pairs converted from the example corpus of the Cosette SQL prover.

* Source: https://github.com/uwdb/Cosette, `examples/*/*.cos`
* Commit: `7a951aa3b1a70a8b310c676e524740b7114975c3`
* Licence: BSD-2-Clause, (c) 2017 The Cosette Team (see `LICENSE`)
* Converter: `tools/cosette_to_sql.py` (regenerate with
  `python tools/cosette_to_sql.py --src <Cosette checkout>`; needs `duckdb`)
* Test: `tests/test_cosette_fixtures.py`

`python tools/cosette_bench.py` scores them (`docs/evals/sqlsolver.md#cosette-and-spes-fixtures`).

## Files

| file | content |
| --- | --- |
| `cosette_cases.jsonl` | one case per line: `name`, `source_dir`, `source_file`, `label`, `schema` (tables with `columns` (`name`, `type`, Cosette's `source_type`, `nullable`, `hidden_predicate`) and `open` for a `??` schema), `constraints`, `sql_a`, `sql_b`, `ddl`, `duckdb` (execution check), plus `constraint_source`, `predicates` and, for `calcite`, `cosette_result` (Cosette's own result from `calcite_result_with_label.csv`) when they apply |
| `cosette_skipped.jsonl` | files not converted: `category` and `reason` |
| `summary.json` | counts per folder and skip category |
| `cosette_adapted.jsonl` | adapted pairs, written by hand and scored apart (`python tools/cosette_bench.py cosette-adapted`): the 4 `sqlrewrites` files skipped for a table with no declared columns, given two `INTEGER NOT NULL` columns per table, and 5 not-equivalent siblings. Fields: `name`, `adapted` (true), `adapted_from`, `source_file`, `source_sha256` (of the `.cos` file at the pinned commit), `label`, `adaptation`, `ddl`, `sql_a`, `sql_b`. Not counted in `summary.json` |

## Labels

| label | folder | meaning |
| --- | --- | --- |
| `equivalent` | `sqlrewrites`, `calcite` | the two queries return the same bag on every database of the schema. `calcite` pairs are Calcite rule rewrites as Cosette transcribed them |
| `not_equivalent` | `inequal_queries` | some database tells them apart; the converter found one on a random DuckDB database for every converted case |
| `conditional` | `conditional` | equivalent only on databases that satisfy `constraints` (`primary_key` on a table's columns). `constraint_source` quotes where the source states the precondition (`examples/conditional/conditional.md` or the `.cos` comment) |

## How the DSL is translated

* **Types and NULL.** Cosette's semantics has no NULL, so every column is
  `NOT NULL`. `int` and the generic types `ty`, `ty0`, `ty1` become `INTEGER`;
  `str`/`string` become `VARCHAR`. An equivalence over a generic type holds for
  `INTEGER` in particular. The converted `not_equivalent` cases use only
  concrete types.
* **Open schemas.** `schema s(a:int, ??)` (more columns, unknown) keeps only
  its declared columns (`open: true` records the `??`). A table with no
  declared column cannot be written in SQL, so such files are skipped unless
  a predicate gives the table a column.
* **Uninterpreted predicates.** `predicate b(s)` becomes a hidden
  `BOOLEAN NOT NULL` column `__b` on every table of schema `s`, and `b(x)`
  becomes `x.__b`. This is sound for proofs: an original instance (rows plus a
  predicate) maps to one of the richer schema by storing `b(row)` in `__b`, so
  a proof over the richer schema implies the original equivalence. The
  converse does not hold: in the richer schema two rows equal on the visible
  columns can disagree on `__b`, which the original predicate (a function of
  the visible row) forbids. **A counterexample found for an `equivalent` case
  with `predicates` must be re-checked for `__b` being a function of the
  visible columns of its row** before it counts against the prover or the
  label. Predicates over two rows (`b(sa, sb)`, `θ(s2, s1)`) are not translated.
* **SQL text** is copied from the backticks unchanged apart from the predicate
  calls. Calcite's generated names such as `$f0` are kept; the DuckDB check
  quotes them. Output columns are compared by position (Cosette ignores output
  names).

## Result

86 `.cos` files: **60 converted**, 26 skipped.

| folder | converted | skipped |
| --- | ---: | ---: |
| `calcite` | 33 | 14: 6 empty `(VALUES)` relation, 5 disagree on random databases, 2 type mismatch, 1 DuckDB error (`SINGLE_VALUE`) |
| `conditional` | 5 | 4: 3 precondition not stated, 1 precondition not expressible as keys |
| `inequal_queries` | 5 | 2: 1 malformed SQL (`344Q2`), 1 disputed label (`344Q1`) |
| `sqlrewrites` | 17 | 5: 4 table with no declared columns, 1 two-row predicate |
| `to_be_supported` | 0 | 1: two-row predicate (and `SEMIJOIN` syntax) |

Notes on the skips that are not plain translation limits:

* `calcite/testAddRedundantSemiJoinRule`, `testPushSemiJoinPastJoinRuleLeft`:
  Cosette wrote Calcite's semi-join as an inner join, which repeats rows unless
  `emp.empno` and `dept.deptno` are keys (they are in Calcite's catalog, not in
  the `.cos` file). `testPushSemiJoinPastJoinRuleRight` and
  `testSemiJoinRuleExists` disagree even with those keys (the latter's two
  sides have different column counts).
* `calcite/testDecorrelateTwoExists`: the `.cos` `emp` schema lists `comm`
  before `sal`, while the rewritten query spells out `sal, comm`, so the
  positional results differ.
* `calcite/testRemoveSemiJoin*WithFilter`: compare `ename`/`name`, declared
  `int`, with the string `'foo'`.
* `inequal_queries/344Q1`: filed as not equivalent, but `w := v` satisfies
  `q1`'s extra conjuncts, so under `DISTINCT` the queries are equal.
* `conditional/fkPennTR` needs "Security uses only its own employees", which
  keys and foreign keys cannot say; `index_sigmod82`, `inline-exists` and
  `missing-pred` state no precondition at all.

## Overlap with the other Calcite corpora

`tests/fixtures/calcite_overlap.json` (written by
`python tools/calcite_overlap.py --spes <SPES> --cosette <Cosette>`) lists, for
every Calcite `RelOptRulesTest` name, the corpora holding a pair for it, with
unique-name counts and pairwise overlaps.

SPES `testData/calcite_tests.json`, Cosette `examples/calcite/calcite_tests.json`
and SQLSolver's `tests/fixtures/sqlsolver/calcite_pairs.txt` are the same 232
tests in the same order: all 232 SPES and Cosette names agree by position
(after dropping SPES's trailing `*` on 3 names and taking Cosette's name for
position 107, which SPES mislabels as a second `testPushMinThroughUnion`), 184
of the pairs are identical, and at 195 positions SQLSolver's q1 or q2 equals
SPES's modulo case, whitespace and alias names (the others are SQLSolver's
edits of the same test). SQLSolver's unnamed pairs therefore take SPES's names
by position.

| corpus | unique names |
| --- | ---: |
| SQLSolver Calcite (= SPES = Cosette json) | 232 |
| QED (`qed_calcite_pairs.jsonl`, branch `claude/rbot-prover-1`, not on master) | 375 |
| R-Bot | 45 |
| Cosette `.cos` converted here (`calcite`) | 33 |
| SPES-only pairs added here (`tests/fixtures/spes`) | 34 |
| all corpora | 430 |

| overlap | names |
| --- | ---: |
| SQLSolver & QED | 181 |
| SQLSolver & R-Bot | 21 |
| R-Bot & QED | 39 |
| Cosette & QED | 31 |
| Cosette & R-Bot | 1 |
| SPES-only & QED | 29 |
| SPES-only & R-Bot | 4 |
| SPES-only & Cosette | 7 |

Only in QED: 174 names; only in R-Bot: 4. Every Cosette and SPES-only name is
already in SQLSolver's list, so the new fixtures add different SQL for tests
already scored, not new tests. No VeriEQL fixture exists on `origin/master`.
Pinned sources: SPES `8049f98d8e64a5c2c43d1ed581e7d5b6e0a006e2`, Cosette
`7a951aa3b1a70a8b310c676e524740b7114975c3`, QED
`31f4b6c271440942ecaca1e1111d4beeabf1f14c` (read from KumoSQL
`1cfce4a0468b2b0fca224676089fbd2727485949`); SQLSolver and R-Bot as in this
repository.
