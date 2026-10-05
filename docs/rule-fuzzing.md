# Rule-level differential fuzzing

[All docs](README.md)

`prove_equivalent_algebraic` compares the normal forms of two queries, so one unsound rewrite in
`algebraic_equivalence.normalize` is a false proof waiting for the right pair. Pair-level fuzzing
(`tools/soundness_fuzz.py`, [proof re-check](proof-recheck.md)) finds a wrong proof only when it happens to generate
a pair that meets the bad rule. `tools/rule_fuzz.py` checks the rewrites one at a time instead: on every query it
runs, each rewrite that fires is checked on its own, on the exact query it saw.

## How it works

1. `Tracer` wraps every rewrite `normalize` calls (whole-tree passes, per-node `step` rules, fixpoint passes and the
   final canonicalization; `rule_names()` lists them by reading `normalize`'s code). It runs `normalize` on a case
   twice: a cheap probe pass fingerprints each call, then a record pass snapshots only the calls that changed the
   tree. A firing is the whole query just before and just after that rewrite.
2. Both queries run on DuckDB databases built for the case's typed schema and constraints (keys, NOT NULL, foreign
   keys): seeded random ones, NULL-heavy, duplicate-heavy, empty, one-row, tie-heavy, large-integer ones, using the
   query's own constants.
3. A difference counts only if all of these hold:
   - `kumosql.duckdb_load.run_unoptimized` gives the same bags (DuckDB's optimizer bug, #347);
   - the bags stay the same with every table's rows reversed (no dependence on row order);
   - every `LIMIT` is stable: the rows equal those with its `ORDER BY` extended by all output columns, ascending and descending (a cut
     among tied rows is unspecified, not a bug);
   - both queries return the same column types (DuckDB coerces a mixed-type `UNION` that BigQuery rejects, and the
     prover does not compare result types).
4. Runtime errors on either side are "unchecked", never a bug: the prover does not model them. Numbers compare
   exactly (an integer and a float need the same value); non-integral floats compare to 10 significant digits.
   BigQuery's `COUNTIF` is 0 over no rows and DuckDB's `count_if` is NULL, so it is rewritten to `COUNT(CASE ...)`.
5. A confirmed difference is reduced to a small witness (query, then rows) while the same rule still fires and still
   changes the result.

Because the firing is checked on the tree the rule saw, a difference is a bug in that rule and nowhere else.

## Using it

```shell
python tools/rule_fuzz.py run --corpus gen --seed 1 --count 1500 --jobs 4 --reduce 3 --out run.json
python tools/rule_fuzz.py run --corpus evals --jobs 4 --out evals.json
python tools/rule_fuzz.py report run.json                   # fired / checked / bugs per rule
python tools/rule_fuzz.py show run.json --rule distinct_rules
python tools/rule_fuzz.py query "SELECT ..." --schema t:id=INT64,x=INT64 --key t:id
python tools/rule_fuzz.py rules                             # every rule it traces
```

Corpora: `gen` (`tools/rule_fuzz_gen.py`, a typed random BigQuery query generator biased toward the shapes behind past
false proofs: shadowed aliases and scopes, global aggregates, ROLLUP/CUBE/GROUPING SETS, DISTINCT and DISTINCT ON,
windows and QUALIFY, set operations with ORDER/LIMIT tails, correlated subqueries, CTEs that shadow tables, USING
joins), `fuzz` (both sides of `tools/soundness_fuzz.py` template pairs), `evals` (the non-held-out queries of the
SQLSolver, QED, mined Calcite, R-Bot, TPC-H and TPC-C evals; held-out pairs are never read) and `target:<module>`
(`tools/rule_fuzz_targets/<module>.py`, template generators that expose `cases(seed, count)`; `count` is per module). The modules are `aggregates` (aggregate, eager-aggregation, regrouping and keyed rules), `distinct_sets` (DISTINCT, dedup joins, set operations and set splits; `_setop_templates.py` adds the ALL and DISTINCT chains, EXCEPT ALL and INTERSECT ALL multiplicities, operands that keep an ORDER BY / LIMIT on their parentheses, empty and constant branches, GROUP BY operands, derived set operations under filters, joins, aggregates and IN / EXISTS, CASE join keys, BY NAME and CORRESPONDING, mixed column types, and cut branches of a UNION ALL; a template starting with `@duckdb ` is read in the DuckDB dialect), `outer_joins`, `grouping_windows` (grouping sets, windows, QUALIFY, LIMIT rules, empty relations) and `scalars` (casts, integer division, dates, LIKE, quantified comparisons, scalar subqueries, UNNEST, constant folding). A template is SQL with `{a|b|c}` choice groups (`tools/rule_fuzz_targets/_base.py`). A query
that `normalize` cannot print faithfully (`LossySql`) still has its earlier firings checked.

`tests/fixtures/rule_fuzz/known_rule_bugs.json` lists open bugs the run should not fail on; a thread that fixes one
deletes its entry. `tests/test_rule_fuzz.py` checks that the harness sees an unsound rewrite, ignores a `LIMIT` cut
among ties, and finds nothing on a small seeded generated run.

## Limits

- Evidence, not proof: a rule is cleared only on the databases and queries tried. The report's fired and checked
  columns say how much each rule was exercised; a rule that never fires has not been tested.
- DuckDB is the oracle, not BigQuery. Where they differ (integer division, string collation, casts) the harness
  declines or approximates, and each difference is reviewed by hand before it is called a bug.
- A rule that fires only inside a subquery in an outer join's `ON` stays unchecked (DuckDB cannot run it).

## Found so far

See the workstream issue ([#496](https://github.com/walterogozaly/KumoSQL/issues/496)) for the running list. The
first runs found two bugs in the empty-relation rules (a grouping with a grand total called empty over empty input;
a dropped left join replaced a same-named column in a nested query), one in UNION ALL column pruning (a
branch lost its only aggregate and went from one row to a row per input row) and one in `_drop_group_in_membership_tests`
(`EXISTS (SELECT 1 FROM u GROUP BY ())` lost its grand-total row). All four are fixed with regression tests.
About 8,000 template cases over the aggregate, distinct and set, outer-join, grouping and window, and scalar
generators fired about 70 rules and found nothing else.

The set-operation sweep ([#518](https://github.com/walterogozaly/KumoSQL/issues/518)) found eight more, all fixed with
regression tests:

- `merge_same_source` and `set_operation_to_exists` unwrapped the parentheses of an operand and lost an `ORDER BY` /
  `LIMIT` that sqlglot keeps on them (`((SELECT x FROM t) ORDER BY x LIMIT 1) INTERSECT DISTINCT ...` became a plain
  filter), `push_filter_into_set_operation` pushed a filter below such a cut, and `collapse_counted_intersection`
  read a cut `UNION ALL` operand through the same parentheses (`tests/test_set_operand_tail_rules.py`).
- `split_distinct_select` found the `x IN (SELECT CASE ...)` it had judged by comparing SQL text in its copy, so a
  look-alike test in a nested select was split instead, where only TRUE counting no longer holds. The same
  text lookup was replaced in the other `set_split_rules` splits (`tests/test_set_split_scope.py`).
- `drop_dedup_read_as_set` dropped the `DISTINCT` of a `UNION ALL` branch because an outer `DISTINCT` reads the union as
  a set, but a `LIMIT` or `OFFSET` on the union counts the branch's repeats: `SELECT DISTINCT d.x FROM ((SELECT DISTINCT
  x FROM t) UNION ALL (SELECT k FROM u) ORDER BY 1 LIMIT 3) AS d` kept three different values and lost all but one
  (`tests/test_distinct_read_as_set_cut.py`).
- `positionalize` (the rewrite of `BY NAME` / `CORRESPONDING` to a positional operation, run before every prover) cut
  a dropped column from a branch's select list in place, so `LEFT UNION ALL BY NAME SELECT DISTINCT y AS x, id AS z`
  became `SELECT DISTINCT y AS x`, which keeps fewer rows, and an `ORDER BY z` or `QUALIFY` on the dropped alias
  pointed at nothing. A branch that loses a column and has a `DISTINCT`, grouping, ordering or cut is now selected
  from by name instead (`tests/test_by_name_dropped_column.py`). This rewrite runs outside `normalize`, so
  `tools/rule_fuzz.py` does not trace it; it was found by reading.
- `set_operation_to_exists` read the operands of `SELECT AS STRUCT x, y ... INTERSECT DISTINCT SELECT AS STRUCT k, w ...`
  as two columns each and returned two columns where the query returns one struct, so the prover called the struct
  query equal to its fields as plain columns. It now declines an operand with `AS STRUCT` or `AS VALUE`
  (`tests/test_set_operation_struct_kind.py`). DuckDB cannot run this rewrite, so the harness reports it as unchecked; found by
  asking whether each shape-changing rule keeps `SELECT AS STRUCT` (`kind`) in mind.
