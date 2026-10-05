# Bounded verification (z3, at most N rows per table)

[Plain-language version](../../docs_simple/evals/bounded-verification.md)

`kumosql.bounded_equivalence` checks that two queries return the same bag of rows on **every database with at most N rows per table**, with symbolic values, so the solver covers every combination of values and NULLs up to the bound. It is a third evidence level, next to an unbounded proof and agreement on executed random databases:

| Evidence | What it says | Where it comes from |
| --- | --- | --- |
| Unbounded proof | equal on every database that meets the declared facts | `algebraic_equivalence`, `smt_equivalence` ([sqlsolver.md](sqlsolver.md)) |
| **Bounded, N rows** | equal on every database with at most N rows per table; says nothing about larger ones | `bounded_equivalence` (this page) |
| Executed datasets | equal on the random databases that were run | `counterexample`, `result_equivalence` |
| Counterexample | a database, replayed on both queries, on which they differ | any of the above, after a replay |

Bounded is not a proof and is never reported as one. The approach is the one VeriEQL describes ("VeriEQL: Bounded Equivalence Verification for Complex SQL Queries with Integrity Constraints", OOPSLA 2024, https://github.com/VeriEQL/VeriEQL): tables of N symbolic tuples, queries as symbolic relations, constraints as assertions. The code here is written from the paper and shares none of VeriEQL's source, which is CC BY-NC-SA 4.0 (the same reason `docs/verieql.md` reads only its benchmark files).

## How it works

Each table has N slots. A slot has a presence flag and, per column, a symbolic value and a NULL flag. Slots fill from the front (a present slot implies the one before it is present), which removes symmetric models. A query becomes a *relation*: a list of rows, each with a presence flag and symbolic cells.

- `WHERE`, `ON`, `HAVING`: conjoin the row's presence with the condition (three-valued logic: NULL does not pass).
- Joins (inner, left, right, full, cross, `USING`, `NATURAL`): every pair of rows, with an outer row for each unmatched side.
- `GROUP BY`: slot *i* is a group representative when it is present and no earlier present slot has the same key (NULLs equal each other); aggregates run over the members. Without `GROUP BY`, an aggregate query always returns one row. A column that is neither grouped nor aggregated is an *arbitrary pick* from the group: a fresh choice constrained to a member, so two queries are equivalent only if they agree whatever the pick.
- `DISTINCT`, `UNION [ALL]`, `INTERSECT [ALL]`, `EXCEPT [ALL]` (branches matched by position; `BY NAME` and `CORRESPONDING` are first rewritten to the positional form by `kumosql.set_operations`, and the answer is unknown when a branch's columns are not known), `IN`, `NOT IN`, `EXISTS`, correlated and scalar subqueries, CTEs, `CASE`, `COALESCE`, `NULLIF`, `LIKE` (literal patterns), arithmetic, `ROUND`, date arithmetic in days, `ORDER BY ... LIMIT ... OFFSET`, window functions (see [Window functions and frames](#window-functions-and-frames)).
- Uninterpreted predicates such as `B(X)` (VeriEQL's symbolic predicates) become z3 functions, so equivalence has to hold for every meaning of `B`.
- Constraints: NOT NULL, keys, foreign keys, enum values, consecutive-id columns and cross-row predicates become assertions.

Two relations are compared as bags: a difference exists when some row's multiplicity differs. `sat` gives a model; `unsat` is "equivalent within the bound".

Anything the encoding does not model (`GROUP_CONCAT`, regular expressions, `UPPER`, date parts, `GROUPING SETS`, recursive CTEs, window frames outside the list below (`GROUPS`, `EXCLUDE`, a frame without `ORDER BY`, a computed offset, a `RANGE` offset over several keys or a non-number key), MySQL's `date + 1`, a `LIMIT` or `OFFSET` on a set operation itself, `SELECT * EXCEPT/REPLACE/RENAME/ILIKE`, a table missing from the schema) raises `Unsupported` and the answer is **unknown**, never a verdict.

## Window functions and frames

Rows of a window's partition sort by its `ORDER BY` keys (NULLs placed as the dialect or an explicit `NULLS FIRST/LAST` says), **ties broken by row position**: the earlier slot comes first. DuckDB on one thread breaks ties by storage order, and the encoding is tested against it with ties in the data.

| Function | Model |
| --- | --- |
| `ROW_NUMBER`, `RANK`, `DENSE_RANK`, `PERCENT_RANK`, `CUME_DIST` | position (or tied-group count) in the partition; the last three give tied rows the same value, whatever the tie-break |
| `NTILE(n)` | literal `n`; the first `size mod n` buckets get one extra row; rows past the end of a short partition get one bucket each |
| `LAG`, `LEAD` (offset, default) | the row that many positions before or after; no frame |
| `SUM`, `COUNT`, `AVG`, `MIN`, `MAX` | over the frame; an empty frame gives NULL (`COUNT` gives 0) |
| `FIRST_VALUE`, `LAST_VALUE`, `NTH_VALUE`, with `IGNORE NULLS` | the first, last or n-th row of the frame (with `IGNORE NULLS`, of the frame's non-NULL rows); NULL when there is none |

Frames: with no frame clause the frame is `RANGE BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW` (the whole partition without `ORDER BY`). An explicit `ROWS` or `RANGE` frame needs an `ORDER BY` and may use `UNBOUNDED PRECEDING/FOLLOWING`, `CURRENT ROW` and `n PRECEDING/FOLLOWING`, one bound or `BETWEEN` two.

- `ROWS n` counts positions in the sort order, so on tied rows the frame depends on the tie-break.
- `RANGE CURRENT ROW` reaches the first or last tied row. `RANGE n PRECEDING/FOLLOWING` takes rows whose key is within `n` of the current key (reversed under `DESC`) and needs one numeric `ORDER BY` key; a whole-number key takes a whole-number offset, a `FLOAT64` key any literal. For a NULL key the offset bounds are its tied NULL rows; for a number, NULL rows are inside the bound or outside it according to where the ordering puts NULLs.
- A frame whose start lies after its end is an error in BigQuery, so it is `Unsupported` here rather than an empty frame; so are `GROUPS`, `EXCLUDE`, a computed or fractional (`ROWS`) offset, and an explicit frame on a function that takes none (`LAG`, `ROW_NUMBER`, `NTILE`, ...).

A `RANGE` frame holds whole groups of tied rows, so an aggregate over it does not depend on the tie-break. A `ROWS` frame, `FIRST_VALUE`/`LAST_VALUE`/`NTH_VALUE` (which pick one tied row), `LAG`, `LEAD`, `ROW_NUMBER` and `NTILE` do, which is why a difference found there is reported only if it survives the replay's shuffles. The NULL-key `RANGE` behaviour is checked against DuckDB; BigQuery's own result for a NULL key under an offset bound was not run.

## Replay, assumptions, and what a result means

A counterexample is returned only after both queries were executed on DuckDB over the model's database, the two result bags differ, and the difference survives three shuffles of every table's rows (so a `LIMIT` tie, or MySQL's arbitrary pick of an ungrouped column, never produces one). A model the replay does not confirm gives `unknown`, after two retries with printable strings and quarter-step reals.

Column domains: a `NUMERIC(p, s)` column holds only values with `s` decimals and `|value| < 10**(p - s)`, and a `DATE` or `DATETIME` / `TIMESTAMP` column only values from year 1 to year 9999, so the solver cannot pick a value the replayed database cannot hold. (Until 2026-10 a model date below year 1 was clamped to `0001-01-01` when decoded, so two rows that differ only in a DATE key column came out equal and the counterexample repeated the primary key; the range is now a constraint instead and nothing is clamped.)

Assumptions reported with every result: bounded databases only; exact arithmetic (no `FLOAT64` rounding or integer overflow); runtime errors are not modeled (division by zero gives NULL, a scalar subquery with several rows takes the first); strings compare case-sensitively; results are compared as bags; ties in `ORDER BY` (`LIMIT`, `ROW_NUMBER`, `LAG`, `LEAD`, `FIRST_VALUE`) are broken by row position, so a difference that depends on the tie-break is reported only if it survives the replay's shuffles, otherwise the answer is unknown.

`check_bounded` tries bounds 1, 2, ..., N in turn, so a counterexample is reported at its smallest size and a timeout still reports the largest bound that finished (`bounded, 2 rows`).

## Testing the encoding

A wrong encoding could hide a difference, so the encoding itself is tested against DuckDB:

- `tests/test_bounded_equivalence.py` compiles 30 query shapes (joins, groups, windows, set operations, subqueries, NULL cases), pins the symbolic database to random concrete ones and compares the result with DuckDB's.
- The same file runs 260 random window queries (24 functions, `ROWS` and `RANGE` frames with every bound kind, ascending and descending keys with explicit and default NULL placement, partitions) over tables of up to 5 rows with repeated key values and NULLs, against DuckDB with `SET threads=1`. At least 150 are modeled and every one matches; a frame the encoding declines is skipped, never compared.
- `python tools/bounded_bench.py differential SUITE` does the same on every query of a VeriEQL suite. Any mismatch is a bug and must stay 0 before a bounded number is quoted.
- `run` cross-checks every bounded verdict: a counterexample against a pair the unbounded prover proved, VeriEQL's published counterexample (replayed on DuckDB) that fits inside the bound, or a random-search counterexample that fits inside the bound each count as **wrong**.

## In the app

Settings → Solver has a "rows per table, bounded check" setting (0 turns it off; the default is 3). **Compare queries** and **Compare tables** show the result as its own label, "bounded, 3 rows", or "different results" with the database, next to the unbounded answer. The check runs when nothing was proven, on the columns, types, NOT NULLs and keys of the saved BigQuery catalog, so queries over tables without a saved schema stay unknown.

## Running it on the evals

```
python tools/bounded_bench.py run literature --rows 3
python tools/bounded_bench.py run calcite --rows 3
python tools/bounded_bench.py run leetcode --rows 3 --every 24
python tools/bounded_bench.py run sqlsolver-calcite --rows 3     # also sqlsolver-spark, -tpch, -tpcc, qed, rbot, cosette, spes
python tools/bounded_bench.py run singh --rows 3
python tools/bounded_bench.py differential calcite
```

Each run first takes the eval's own baseline verdict (unbounded prover and executed search), then the bounded check, and prints the two side by side. The scoreboard rows are in `benchmarks/results/bounded-*.json`.

## Results

Measured 2026-10-02 at 3 rows per table.

| Suite | Pairs | Bounded, 3 rows | Different (replayed) | Timeout | Unsupported | Unknown |
| --- | --- | --- | --- | --- | --- | --- |
| SQLSolver Calcite | 232 | 212 | 0 | 1 | 18 | 1 |
| SQLSolver Spark SQL | 127 | 106 | 0 | 0 | 20 | 1 |
| SQLSolver TPC-H | 22 | 7 | 0 | 4 | 11 | 0 |
| SQLSolver TPC-C | 19 | 19 | 0 | 0 | 0 | 0 |
| QED Calcite | 375 | 363 | 0 | 3 | 9 | 0 |
| R-Bot Calcite | 45 | 23 | 0 | 0 | 22 | 0 |
| Cosette examples | 60 | 52 | 6 | 0 | 2 | 0 |
| SPES Calcite | 34 | 26 | 3 | 0 | 5 | 0 |
| Singh & Bedathur LeetCode pairs | 1006 | 824 | 71 | 97 | 13 | 1 |
| VeriEQL Literature | 64 | 28 | 24 | 4 | 4 | 4 |
| VeriEQL Calcite-397 | 397 | 276 | 2 | 4 | 112 | 3 |
| VeriEQL LeetCode (1,000-case sample) | 1000 | 452 | 231 | 123 | 179 | 15 |

Every row has 0 wrong. A *timeout* is a pair whose 3-row check timed out, some after finishing a smaller bound (reported as e.g. "bounded, 2 rows"); *unknown* is a model the replay did not confirm or a parse failure. For pairs the unbounded prover already proves, the bounded verdict is a cross-check of the encoder. On pairs the proofs leave unknown, the bounded check refutes pairs the 150-database random search missed (LeetCode sample: 90 refutations on pairs the executed search called "agrees"; Literature: 6 of 25), each replayed on DuckDB. Singh & Bedathur's 1,794 pairs the prover already refutes are not rerun.

Disputed labels: Cosette's `testDecorrelateTwoIn` and SPES's three semi-join cases (`testSemiJoinRule`, `testSemiJoinRuleExists`, `testSemiJoinTrim`) are labelled equivalent, but a replayed bounded counterexample exists; they are counted as label disputes, not as wrong.

Checks behind "0 wrong": the encoding agrees with DuckDB on 92 Literature, 511 Calcite and 1,504 LeetCode-sample queries (`differential`, 0 mismatches; queries with an ungrouped column are excluded because their answer is arbitrary). The first LeetCode run found the encoder assumed ORDER BY ties away, which hid real differences in 18 pairs; ties are now broken by row position and the same run is 0 wrong. A doubled primary key in one family of pairs is read as the shared harness reads it (the later one wins).

The bounded check is developed with these suites in view (tuned on test), and held-out cases are not reserved yet. Caveats per suite are in `benchmarks/results/bounded-*.json`.

## SQLite (SQL-IQ)

`SQLiteReplay` replays on the standard-library `sqlite3` module, for queries in SQLite's dialect. For SQLite only counterexamples are offered: the encoding's `LIKE` (case-sensitive) and `/` (exact) differ from SQLite's, so "no counterexample" is reported as unknown. `tools/sqliq_bench.py` uses it as a last step of the Equivalence Judge, after the random and targeted databases agree: a database of at most 3 rows per table on which SQLite itself returns different results answers "no". On the 1,390 pairs it settles 16 more (15 match the labels, 1 "equivalent" label is a label dispute), moving the score from 1165 to 1179 ([sql-iq.md](sql-iq.md#scores)).
