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

## Repairs

`kumosql.incremental_repairs.propose_repairs(sqlx, target, sources, kinds)` returns, for a model that diverges under a contract, the repairs that make it equal its full refresh under that contract. A repair is a unified diff of the SQLX (`Repair.diff`); nothing is applied or written. Four edits are tried alone and together, smallest first, and a combination that contains an already offered repair is skipped:

| Edit | What it changes | Proven by |
| --- | --- | --- |
| `dedup` | Reads each source whose key the contract can re-deliver through `QUALIFY ROW_NUMBER() OVER (PARTITION BY key ORDER BY ts DESC) = 1`. A model over one table gets the `QUALIFY` on its own query (the shape rule R3 reads); with joins each source is wrapped as a derived table that keeps its alias and its `${ref()}` | R3 or R7 |
| `drop_update_partition_filter` | Removes `bigquery.updatePartitionFilter`, which stops the `MERGE` from matching rows outside the filter, so a changed row lands beside its old version | R7 (every generated-fixture model sets it, so "drop it, plus dedup" is the usual repair) |
| `full_rerun_merge` | Removes the `when(incremental(), ...)` filter so each run executes the full query and merges on the existing `uniqueKey` | R7 |
| `watermark_lookback` | Changes a strict `ts > COALESCE((SELECT MAX(ts) FROM self), d)` to `ts >= TIMESTAMP_SUB(..., INTERVAL 1 HOUR)` and adds the source's key as `uniqueKey` (a `>=` watermark without a key only gets the key). The one-hour lookback is a constant, non-negative interval; its length is the owner's choice and does not change the proof | R2 or R3 |

`full_rerun_merge` and `watermark_lookback` are alternatives and never combined. A repair is offered only when both of these hold, and `RepairReport.refused` lists the others with the reason:

1. **Safe under the contract.** The repaired SQLX is parsed again and `incremental.prove()` must return a proof. A repair no rule proves is not offered, however likely it looks to work. A short counterexample search over the repaired model must also find nothing; a hit would mean a wrong proof, and the repair is dropped.
2. **Full refresh unchanged.** The repaired full query returns what the original does on every source state where each declared key is unique and non-NULL. If the repair leaves the full query's text alone it is immediate. For `dedup` there are two steps. The lemma, checked by `eliminate_key_dedups`: a `QUALIFY ROW_NUMBER() OVER (PARTITION BY k ...) = 1` over one base table whose declared key is inside `k` keeps every row, because each partition has one row (the `ORDER BY` must be plain columns, which cannot fail on that row). Then `prove_equivalent_algebraic`, with every source's key and NOT NULL columns as constraints, proves the original full query equal to the repaired one with those de-duplications removed. The algebraic prover cannot do the first step itself: it keeps a window computation whole.

The monotonicity analysis that R7 uses learned one fact for this: a key de-duplication of a source drops only exact copies (the declared key is unique, `duplicate` re-delivers a row unchanged, `update` changes every copy together), so what is stable without the `QUALIFY` stays stable (`kumosql.incremental_copy_dedup`). It does not apply when the contract allows `null_key`, to a partition that does not contain the key, over a join, a CTE or the model's own table. Without it a filter on a non-key column above the de-duplication, such as `WHERE created_at >= '2020-01-01'`, would hide the key.

**What a repair does not claim.**

* On a source state where the contract has re-delivered rows, the repaired model's full refresh is the original's over the distinct rows. The original's full refresh would hold such a row twice; removing that is what `dedup` is for. Every repair lists this in `assumptions`.
* Dropping `updatePartitionFilter` makes each merge scan the whole table instead of the partitions the filter names, and a full re-run reads every source row each run. These are costs, not correctness, and are not modelled.
* Only these four edits are tried. A model whose output key is not unique (a join or `UNNEST` that fans rows out), whose result depends on tie-breaking, or that needs updates or deletes to be tolerated gets no repair, and the report says which edit was refused and why.

Tests: `tests/test_incremental_repairs.py` (the dev cases of `tests/fixtures/incremental` that diverge, each edit's near misses, a repair that would change the full query being refused, an executed check of the dedup lemma on keyed data, and a generated-fixture model).

## Scanning a project

`python -m kumosql incremental-report PROJECT` (`kumosql-incremental-report`) checks every `type: "incremental"` action under `definitions/` against three contracts: `append_only`, `late_and_duplicate` and `mutable` (updates and deletes). Source columns are inferred from what the query reads (see below), so each answer is about the model on those assumed sources; `--source-schema` supplies real columns. Models it cannot simulate (JavaScript helpers in the query, `pre_operations` that run on every run, unparsable SQL) are listed as unsupported, never guessed.

Each row has one of six outcomes: `safe` (a proof rule applies), `diverges` (a replayable counterexample), **`nondeterministic`** (the full refresh itself depends on tie-breaking, shown by a witness, so there is no single table for the incremental run to equal; it is listed with the window or aggregate that can tie), `unknown` (no divergence found and no proof), `unsupported` and `timeout`. A `diverges` row also carries the **repairs** of the [Repairs](#repairs) section that could be proven, as SQLX patches that are never applied: `--diffs` prints them, `--json` includes them with the proof rule and the full-refresh argument, `--no-repairs` skips the search for them. The summary adds a line per contract: how many diverging models have a proven repair. A diverging model with none lists why each single edit was refused.

**Source inference.** A table is the unit: each table the full query reads gets the columns the query reads from it, a type guessed from the column name, `id` as its unique key and the first timestamp-shaped column as its event time. Columns are found per `SELECT`, so a bare column belongs to the one table its own `SELECT` reads, not only when the whole query reads a single table. A `WITH base AS (SELECT * FROM t)` re-exposes `t`, so columns read from `base` are `t`'s; `a.*` names no column; and `FROM t AS a, UNNEST(a.items) AS item` with `item.qty` and `item.price` makes `items` an `ARRAY<STRUCT<qty INT64, price INT64>>`, which the simulator generates and loads (`kumosql.incremental_sources`, `kumosql.incremental_types`). Before this, a column read through a pass-through CTE was dropped and an unnested column was a scalar, so the model failed to run on every generated state. The search used to skip such a failing state and report `unknown` ("no divergence in 20 sequences") when not one state had run; it now reports `unsupported` ("could not run on any of 20 generated source states", with the first error).

**On the generated fixture project** (`python tools/make_dataform_fixture.py OUT --models 600 --seed 11`; 62 incremental models, every one with `updatePartitionFilter` and a merge on `id`, written by the generator, not a real project). Counts are the same for the three contracts; `repaired` counts diverging models with at least one proven repair.

| | before (R7 merged, no repairs) | after (this change) |
| --- | --- | --- |
| diverges | 33 | 47 |
| unknown | 25 | 0 |
| unsupported | 4 | 15 |
| nondeterministic | 0 | 0 |
| `late_and_duplicate` diverging models with a proven repair | none offered | 38 of 47 (81%) |
| `append_only` diverging models with a proven repair | none offered | 38 of 47 |
| `mutable` diverging models with a proven repair | none offered | 0 of 47 |

What moved. The 25 `unknown` rows were models the simulator could not run, not models it had checked: 9 with an `UNNEST` of an array column inferred as a scalar, 11 reading a struct through `*`, 5 over a pass-through CTE. With the inference above, the 9 `UNNEST` models and the 5 CTE models run and diverge (the unnest fans one `id` out into several rows, so the `MERGE` sees one `id` twice); the 11 struct models stay out of reach (the BigQuery-on-DuckDB layer refuses a `STRUCT` read by `*`) and are now reported `unsupported`, which is what they are. So the fall in `unknown` is mostly a relabelling of models that were never tested, and the 14 new divergences are real findings of the better inference, not a looser test. The success measures set for this work were at most 5 `append_only` unknown (0 here) and repairs for at least 80% of `late_and_duplicate` divergences (81% here: the 9 without one are the `UNNEST` models, whose output has no unique key for any of the four edits to merge on; the other 38 take "drop `updatePartitionFilter`" alone (23) or "drop it and de-duplicate" (15), the latter proven by R7). The inference and the repairs were developed with this fixture in view, so these counts are optimistic; the unseen fixtures below were generated with other seeds and scanned once, afterwards.

Two other generated projects, scanned once after the code was fixed (`--models 600`, seeds 7 and 23), give the same picture:

| | seed 7 before | seed 7 after | seed 23 before | seed 23 after |
| --- | --- | --- | --- | --- |
| incremental models | 64 | 64 | 55 | 55 |
| diverges (any contract) | 39 | 55 | 23 | 39 |
| unknown | 23 | 0 | 29 | 0 |
| unsupported | 2 | 9 | 3 | 16 |
| `late_and_duplicate` diverging models with a proven repair | none offered | 47 of 55 (85%) | none offered | 28 of 39 (72%) |

`unknown` is 0 on all three. The 80% repair share is met on two of the three: the models with no repair are all `UNNEST` fan-outs, so the share follows how many of the project's models the generator made that shape (11 of 39 diverging models for seed 23, 9 of 47 for seed 11). Adding the element offset to the merge key would repair them, but it adds a column, so it would change the full refresh, which a repair may not do.

This is a scan, not a scored eval: the sources are inferred, so there are no labels.

On the real fixture repository (`kumosql-dataform-fixture`, commit f056260, read-only): 316 incremental models, 267 simulated and 49 unsupported (35 always-run `pre_operations`, 13 JavaScript `dateFilter` helpers, 1 unparsable). `append_only`: 265 unknown, 2 timeout (no proof rule covers the fixture's merge-of-a-full-re-run definition). `late_and_duplicate`: 105 diverge (a re-delivered row makes the `MERGE` match one target row twice), 162 unknown. `mutable`: 206 diverge (a deleted or changed source row leaves its old row), 61 unknown. Run took about 17 minutes. Unscored, for the same reason. These counts were taken before R7, the tie witness and the changes above and have not been re-run.

Known limits: a model whose result depends on tie-breaking is `nondeterministic` only when a witness shows it (the same rows stored in a different order give a different result); a tie the simulator cannot provoke stays `unknown` or `diverges`.
