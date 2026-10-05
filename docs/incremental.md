# Incremental models: does the incremental run equal a full refresh?

[Plain-language version](../docs_simple/incremental.md)

A Dataform incremental table runs its full query once, then on later runs only the `when(incremental(), ...)` form, appending (or merging on `uniqueKey`). It is correct when, after every batch of source changes, the table equals what a full refresh would produce. `kumosql.incremental` checks that.

```python
from kumosql.incremental import SourceTable, check_incremental, parse_incremental_sqlx

model = parse_incremental_sqlx(sqlx_text, target="orders_inc")       # ${self()} resolves to this
sources = {"events": SourceTable({"id": "INT64", "ts": "TIMESTAMP", "v": "INT64"}, key=("id",), time_column="ts")}
verdict = check_incremental(model, sources, kinds={"insert_new", "insert_late"})
verdict.outcome          # "safe" | "diverges" | "unknown" | "unsupported" | "timeout"
verdict.counterexample   # for "diverges": initial rows and batches that replay the failure
```

## What it models

The simulator (DuckDB) follows Dataform's run cycle:

* The first run builds the table with the full query; later runs apply the incremental query.
* Incremental `pre_operations` run first (for example a `DELETE FROM ${self()} ...` reload window).
* Without `uniqueKey` the result is appended. With it the run is Dataform's BigQuery `MERGE`: matched target rows are updated in place (two copies of a row in the table stay two copies), unmatched source rows are inserted, a target row matching more than one source row is an **error** (BigQuery raises), and NULL keys never match, so such rows are inserted again every run.
* `bigquery.updatePartitionFilter` is added to the `MERGE` condition as Dataform writes it (`AND DATAFORM_DEST.<filter>`), so a target row outside it is never matched and a changed row lands beside its old version. The proof rules do not apply to such a model; only the counterexample search does.
* Settings that change the run and are not modelled make the model `unsupported`, never `safe`: an `insert_overwrite` (or any other explicit) `incrementalStrategy` that is not the default for the model, `incrementalPredicates`, `post_operations`, `pre_operations` that run on full builds, and a `uniqueKey` or `updatePartitionFilter` that is not a literal value.
* `CURRENT_TIMESTAMP` is pinned to a per-run clock; audit columns are excluded by name (`ignore_columns`) rather than making every run differ.
* `${self()}`, `${ref()}`, `when(incremental(), a, b)` and `when(!incremental(), a)` are evaluated; any other interpolation is refused (`unsupported`) rather than guessed. SQLX splitting reuses `kumosql.sqlx`.

Source changes are DuckDB/ANSI DML; model SQL is BigQuery. DuckDB is where results are executed, so BigQuery behaviour that DuckDB does not share (type coercion, `MERGE` rules) is modelled explicitly as above or not claimed.

## Contracts

A contract is the set of source-change kinds allowed: `insert_new`, `insert_late`, `insert_boundary` (same event time as the newest), `duplicate` (exact re-delivery), `update`, `update_touch` (update that also moves the event time to the newest), `delete`, `null_key`, `empty` (a run with no change). `tables` restricts which source tables change. "Safe" always means *under that contract*; there is no contract-free correctness.

## Verdicts

* **safe**: a proof rule applies. R1 (append): no `uniqueKey`, strict watermark `ts > COALESCE((SELECT MAX(ts) FROM self), <old date>)`, row-wise single-table query, only in-order inserts. R2 (merge): `uniqueKey` equal to the source's declared key, `>` with `insert_new`/`update_touch` (`update_touch` only without a `WHERE`, since an update can move a row out of the filter and a merge never deletes it), or `>=` or a `TIMESTAMP_SUB` lookback also allowing `insert_boundary`. The watermark must be exactly `COALESCE((SELECT MAX(ts) FROM self), <literal date no later than 2000>)`: a `WHERE`, `HAVING`, join, `LIMIT`, sample, snapshot or another dataset's table of the same name in the subquery, or a computed default, is not a watermark (`HAVING FALSE` makes the `MAX` NULL, so every run re-reads everything). A lookback must subtract a constant, non-negative interval (`INTERVAL -1 DAY` raises the boundary). Assumptions are in the `prove_watermark` docstring. Further rules are in `kumosql.incremental_rules`:
  * R3 (key de-duplication): R1 or R2 plus `QUALIFY ROW_NUMBER() OVER (PARTITION BY <source key> ...) = 1`; re-delivered rows (`duplicate`) are then absorbed.
  * R4 (truncated watermark): R2 whose watermark is `DATE(ts)`, `TIMESTAMP_TRUNC(ts, ...)` or `CAST(ts AS DATE)` `>=` the `MAX` of the same expression projected by the model, so a run reloads the newest day. A strict `>` is refused.
  * R5 (group re-aggregation): `SELECT g, <SUM/COUNT/MIN/MAX/AVG>, MAX(ts) AS m ... GROUP BY g` merged on `g`, whose incremental run keeps the groups with a row at or after the watermark on `m` (`g IN (SELECT g FROM source WHERE ts >= ...)`). Inserts only; no `HAVING`. Assumes `g` is never NULL (the change kinds only make NULLs through `null_key`, which is refused).
  * R6 (unchanged joined tables): R1 over a fact table joined (`INNER` or `LEFT`, fact table first) to tables the contract's `tables` never changes.
* **diverges**: a random search over change sequences allowed by the contract found one on which the model differs from a full refresh (or fails), shrunk by dropping batches and statements while it still diverges. It is returned with the verdict and replays on its own.
* **unknown**: no proof rule applies and the search found nothing. This is bounded evidence, not a proof.

## R8: delete-then-reload windows

Many models make late rows safe by deleting the table's newest window in `pre_operations` and reloading it:

```sql
pre_operations { ${when(incremental(), `DELETE FROM ${self()} WHERE ts >= TIMESTAMP_SUB((SELECT MAX(ts) FROM ${self()}), INTERVAL 2 HOUR)`)} }
SELECT id, ts, v FROM ${ref("events")}
${when(incremental(), `WHERE ts > COALESCE((SELECT MAX(ts) FROM ${self()}), TIMESTAMP '1970-01-01')`)}
```

The pre-operation runs in the same script as the table statement, so the watermark reads the table *after* the delete and reloads what the delete took away. `kumosql.incremental_reload` (rule R8, hooked into `prove_more`) proves two shapes over a row-wise single-table query. The theorem, its proof and the near misses are in the module docstring.

* **Watermark read after the delete.** The delete is `tc >= W` (or `tc > W`) with a bound that does not depend on the row: a constant, `(SELECT MAX(tc) FROM self)` minus a non-negative constant interval, or a script variable declared before the delete with such a value. The reload is the R1/R2 watermark. What remains after the delete is exactly the rows of the full output at or below the new maximum, so the run equals the full refresh when every change lies above it. Append: `insert_new` and `empty`, plus `insert_boundary` when the delete provably removes the rows at the maximum (`>=` with a non-negative interval, `>` with a positive one); the reload must be strict `>`, since `>=` repeats the kept rows at the new maximum. Merge on the source key: also `update_touch` (only without a `WHERE`), and `insert_boundary` also when the reload is `>=` or a lookback.
* **Window reloaded through a variable.** `DECLARE w DEFAULT (COALESCE(MAX(...) - c, <old date>))` before `DELETE ... WHERE tc >= w` and `WHERE tc >= w` in the query. `w` is read before the delete, so `effective_model` does not substitute it and R8 reads the raw `pre_operations`. It needs `w` non-NULL (the `COALESCE`) and `w` at most the table's maximum, and the delete and the reload must cover the same rows (append) or the reload must cover the deleted rows (merge).

Refused: late rows, re-delivered rows, `update` and `delete` of source rows, `update_touch` without a key or with a `WHERE`, `insert_boundary` when the delete bound is a constant or can equal the maximum, a bound or watermark read from the source table instead of the table itself, a delete with a further condition, a variable that can be NULL, and an append that reloads with `>=`. The dev case `pre-operation-reload-own-window` is now proven; `-late-beyond-window`, `-duplicate-old-row` and `-update-touch` stay refuted. Proven shapes are also run through `search_divergence` with many seeds in `tests/test_incremental_reload.py`.

## Scores

`python tools/incremental_bench.py` runs the corpus in `tests/fixtures/incremental/` and reports counts per outcome; the README scoreboard has two rows (detection, evidence `executed`; proofs, evidence `proof`).

| | cases | result |
| --- | --- | --- |
| Diverging cases refuted | 34 | 33, 1 unknown, 0 false alarms |
| Safe cases proven | 15 | 15 proven, 0 false proofs |
| Held-out (12 blind cases) | 12 | 9/9 refuted, 3/3 proven |
| Baseline (first 37 cases, before generalising the proof rules) | 37 | 24/24 refuted, 2/13 proven |

Before R3 to R6, five safe cases were unknown: a de-duplicating merge (`QUALIFY ROW_NUMBER`), a re-aggregating merge, a pre-operation reload window, a day-granular watermark and a join against an unchanged dimension. R3 to R6 prove four of them (written for those cases, so the dev number is optimistic; the held-out cases were not used). The fifth, `pre-operation-window-from-source`, was relabelled `diverges`: it deletes and reloads the last 2 hours before the source's newest row, so a run that adds two rows more than 2 hours apart (allowed by `insert_new`) never loads the earlier one. The change generator steps new rows one hour at a time, so the search does not find it and it stays unknown.

While testing R3, R2 turned out to prove a merge with a `WHERE` under `update_touch`, which diverges when an update makes a row fail the filter (the old row stays). No corpus case had that shape; R2 now refuses it, and `tests/test_incremental_rules.py` keeps it as a regression case alongside near misses for every rule.

**How cases are labelled.** Each case is authored with a label for its contract and a scripted change sequence. A fidelity check replays the script in the simulator and requires the outcome to match the label (this caught two authoring mistakes in the blind set). Detection never sees the script. Authored cases are original; adapted material (pg_ivm, the fixture repository) is recorded separately when added. No existing eval covers incremental maintenance (the SQLSolver, R-Bot, SQL-IQ and rewriting evals compare queries, not run histories); `synthetic_check` checks rewrites on random data and is the nearest related code.

**Caveats.** The proof rules and the change generator were developed on the 37 dev cases, so the dev numbers are optimistic; the held-out set is small. Every failure found is kept as a case.

## pg_ivm workloads

`python tools/incremental_pgivm.py <pg_ivm checkout>` reads pg_ivm's `sql/pg_ivm.sql` and `sql/outer_join.sql` (v1.16, commit 22b4b45, PostgreSQL License) and writes `tests/fixtures/incremental/pgivm_cases.json`. Each case keeps the **original** workload (view query, statements before and after it, verbatim Postgres SQL, file and line) apart from the **adapted** one (DuckDB statements and the Dataform model). Savepoint rollbacks start a new workload from the same base state; exact duplicates are dropped.

pg_ivm maintains views with its own engine, which Dataform lacks. The adapted model is the common Dataform append pattern, the same for every workload and fixed before running: every source table gains a `_loaded_at` load time (insertion order), the model projects the first table's `_loaded_at` (its `MAX` for an aggregate) and each incremental run reads only rows past `MAX(__loaded_at)` of its own table. Inserts into the first table keep it correct; updates, deletes, changes to joined tables and re-aggregation break it.

`python tools/incremental_bench.py --track pgivm`: 1,069 workloads extracted, 336 after de-duplication, **110 adapted**, 226 unsupported (216 use data-modifying CTEs, which apply several changes in one snapshot and cannot be replayed one by one; 10 use CTEs, subqueries, `DISTINCT` or non-table sources). Of the 110: **100 drift on their script and all 100 are refuted again by `check_incremental`, without the script**, 8 agree with their script (not scored: no label), 2 could not be run. The first run, at 20 seeds, refuted 95 and left 5 unknown; the seed count was then raised to 30 (the default), which refutes all 100. **That 100/100 is tuned on test; the clean number is 95/100.** No proof was given for a drifting workload. Held-out: `--track pgivm-heldout` gives each of the 29 distinct adapted views one fresh random insert/update/delete script (generator seed 9001, never seen while tuning) and scores the drifting ones once at the frozen 30 seeds: 28 of 29 drift and all 28 are refuted, 0 wrong.

## Scanning a project

`python -m kumosql incremental-report PROJECT` (`kumosql-incremental-report`) checks every `type: "incremental"` action under `definitions/` against three contracts: `append_only`, `late_and_duplicate` and `mutable` (updates and deletes). Source columns are inferred from what the query reads (`id` taken as the unique key, `*_at` / `ts` as event time), so each answer is about the model on those assumed sources; `--source-schema` supplies real columns. Models it cannot simulate (JavaScript helpers in the query, `pre_operations` that run on every run, unparsable SQL) are listed as unsupported, never guessed.

On the generated fixture project (`python tools/make_dataform_fixture.py OUT --models 600 --seed 11`): 64 incremental models, 54 simulated and 10 unsupported (7 always-run `pre_operations`, 2 JavaScript date helpers, 1 unparsable). Their definition re-runs the full query and merges on `id`. Under `append_only` all 54 are unknown (no proof rule covers merges of a full re-run). Under `late_and_duplicate`, 19 diverge: delivering a row again makes the `MERGE` match one target row twice, which BigQuery rejects. Under `mutable`, 40 diverge: a deleted or changed source row leaves its old row in the table. This is a scan, not a scored eval: the sources are inferred, so there are no labels.

On the real fixture repository (`kumosql-dataform-fixture`, commit f056260, read-only): 316 incremental models, 267 simulated and 49 unsupported (35 always-run `pre_operations`, 13 JavaScript `dateFilter` helpers, 1 unparsable). `append_only`: 265 unknown, 2 timeout (no proof rule covers the fixture's merge-of-a-full-re-run definition). `late_and_duplicate`: 105 diverge (a re-delivered row makes the `MERGE` match one target row twice), 162 unknown. `mutable`: 206 diverge (a deleted or changed source row leaves its old row), 61 unknown. Run took about 17 minutes. Unscored, for the same reason.

Known limits: a model whose result depends on tie-breaking (an `ORDER BY` or `ROW_NUMBER` with equal keys) can differ between two evaluations; the simulator compares what DuckDB returns and does not detect that.
