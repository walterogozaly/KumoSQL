# Incremental models: does the incremental run equal a full refresh?

[Plain-language version](../docs_simple/incremental.md)

A Dataform incremental table runs its full query once, then on later runs only the `when(incremental(), ...)` form, appending (or merging on `uniqueKey`). It is correct when, after every batch of source changes, the table equals what a full refresh would produce. `kumosql.incremental` checks that.

```python
from kumosql.incremental import SourceTable, check_incremental, parse_incremental_sqlx

model = parse_incremental_sqlx(sqlx_text, target="orders_inc")       # ${self()} resolves to this
sources = {"events": SourceTable({"id": "INT64", "ts": "TIMESTAMP", "v": "INT64"}, key=("id",), time_column="ts")}
verdict = check_incremental(model, sources, kinds={"insert_new", "insert_late"})
verdict.outcome          # "safe" | "diverges" | "nondeterministic" | "unknown" | "unsupported" | "timeout"
verdict.counterexample   # for "diverges": initial rows and batches that replay the failure
# for "nondeterministic" the witness is two orders of the same rows that give different results
```

## The run cycle

Dataform builds an incremental table in one of two modes. Writing the table as the state `S` of the sources and the table `T`, one run is

```
S_next = T(S_old, input, mode)
```

where `S_old` is the table before the run, `input` is the sources as they are when the run starts, and `mode` is `full` or `incremental`. The question this module asks is whether, after every run the contract allows, `S_next` equals what `mode = full` would build from the same `input`.

The templates below were read in the `@dataform/cli` 3.0.62 bundle (`cli/api/dbadapters/execution_sql.ts`, compiled into `bundle.js`). They are the SQL text Dataform sends, read offline; nothing was executed against BigQuery, and later Dataform versions may differ.

| | Statement Dataform sends |
| --- | --- |
| Mode | `full` when the table does not exist, is a view, or the run is a full refresh (unless the table is `protected`); otherwise `incremental` |
| Full build | `create or replace table T as <query>`, wrapped by the **non-incremental** `pre_operations` and `post_operations` |
| Incremental, no `uniqueKey` | `insert into T (cols) select cols from (<incremental query>) as insertions`, wrapped by the **incremental** `pre_operations` and `post_operations` |
| Incremental, `uniqueKey` | `merge T DATAFORM_DEST using (<incremental query>) DATAFORM_SOURCE on DATAFORM_DEST.k = DATAFORM_SOURCE.k [and DATAFORM_DEST.<updatePartitionFilter>] when matched then update set <every column> = DATAFORM_SOURCE.<column> when not matched then insert (<every column>) values (<every column>)` |
| Legacy `where` | the incremental query becomes `select * from (<query>) as subquery where <where>` |

Further facts that decide what a run can do:

* The pre-operations, the table statement and the post-operations of one run are joined into **one script**. A `DECLARE` in `pre_operations` is therefore visible to the query, and a `SET` or `DELETE` there has run before it reads the table.
* The columns of the `insert` and the `merge` are the columns of the table that already exists, not of the query.
* The `merge` never deletes. A target row no source row matches stays as it is, and `updatePartitionFilter` makes a target row outside the filter unmatchable, so its changed version is inserted beside it.
* BigQuery raises an error when one target row matches more than one source row; NULL keys never match, so such a row is inserted again on every run.
* `insert_overwrite`, `incrementalPredicates` and `onSchemaChange` change what a run does and are not modelled.

## What it models

The simulator (DuckDB) executes that cycle:

* The first run builds the table with the full query; later runs apply the incremental query.
* Incremental `pre_operations` run first (for example a `DELETE FROM ${self()} ...` reload window); the full build runs the non-incremental ones.
* **Script variables.** `DECLARE x [type] DEFAULT (...)` and `SET x = (...)` in `pre_operations` are evaluated on full and incremental runs alike. They are bound in DuckDB through one-row temporary tables, and a liveness pass skips a declaration nothing reads. Dataform's documented watermark pattern (`DECLARE wm DEFAULT (SELECT MAX(ts) FROM ${self()})`, then `WHERE ts > wm`) is therefore simulated as written, and the proof rules read it through `effective_model`, which replaces each variable by its definition when that is exact: nothing between the declaration and the read writes a table the definition reads, and the definition is not run-dependent (`RAND`, the clock).
* **Data-neutral statements** are recognised and skipped: `GRANT`, `REVOKE` and `ALTER ... SET OPTIONS` in `pre_operations` or `post_operations` change permissions and metadata, not rows. A `post_operations` block holding only these, or script variables, no longer makes the model unsupported.
* Without `uniqueKey` the result is appended. With it the run is Dataform's BigQuery `MERGE`: matched target rows are updated in place (two copies of a row in the table stay two copies), unmatched source rows are inserted, a target row matching more than one source row is an **error**, and NULL keys never match.
* `bigquery.updatePartitionFilter` is added to the `MERGE` condition as Dataform writes it. The proof rules do not apply to such a model; only the counterexample search does.
* Settings that change the run and are not modelled make the model `unsupported`, never `safe`: an `insert_overwrite` (or any other explicit) `incrementalStrategy` that is not the default for the model, `incrementalPredicates`, `post_operations` that change data (an `INSERT`, `UPDATE`, `DELETE` or `MERGE`), `pre_operations` that change data on full builds, and a `uniqueKey` or `updatePartitionFilter` that is not a literal value.
* `CURRENT_TIMESTAMP` is pinned to a per-run clock; audit columns are excluded by name (`ignore_columns`) rather than making every run differ.
* `${self()}`, `${ref()}`, `when(incremental(), a, b)` and `when(!incremental(), a)` are evaluated; any other interpolation (a JavaScript helper) is refused (`unsupported`) rather than guessed. SQLX splitting reuses `kumosql.sqlx`; statements are split on `;` and on SQLX `---` lines.

Source changes are DuckDB/ANSI DML; model SQL is BigQuery. DuckDB is where results are executed, so BigQuery behaviour that DuckDB does not share (type coercion, `MERGE` rules) is modelled explicitly as above or not claimed.

## Contracts

A contract is the set of source-change kinds allowed: `insert_new`, `insert_late`, `insert_boundary` (same event time as the newest), `duplicate` (exact re-delivery), `update`, `update_touch` (update that also moves the event time to the newest), `delete`, `null_key`, `empty` (a run with no change). `tables` restricts which source tables change. "Safe" always means *under that contract*; there is no contract-free correctness.

## Verdicts

* **safe**: a proof rule applies. Each rule is a theorem about the run cycle above, with its conditions checked statically; if any condition cannot be shown, the rule returns nothing and the search runs instead. `check_incremental` tries `prove` first, which reads the model through `effective_model` (script variables substituted) and tries R1 to R7 in order. R1 to R2 are in `incremental.prove_watermark`, R3 to R6 in `kumosql.incremental_rules`, R7 in `kumosql.incremental_merge`.
  * **R1, append.** No `uniqueKey`, strict watermark `ts > COALESCE((SELECT MAX(ts) FROM self), <old date>)`, a row-wise query over one table, only in-order inserts. Each new row's time exceeds every earlier one, so a run appends exactly the full query's output on the new rows.
  * **R2, merge on a watermark.** `uniqueKey` equal to the source's declared key; `>` with `insert_new` or `update_touch`, or `>=` or a `TIMESTAMP_SUB` lookback, which also allows `insert_boundary`. `update_touch` only without a `WHERE`, since an update can move a row out of the filter and a merge never deletes it. The watermark must be exactly `COALESCE((SELECT MAX(ts) FROM self), <literal date no later than 2000>)`: a `WHERE`, `HAVING`, join, `LIMIT`, sample, snapshot or another dataset's table of the same name in the subquery, or a computed default, is not a watermark (`HAVING FALSE` makes the `MAX` NULL, so every run re-reads everything). A lookback must subtract a constant, non-negative interval (`INTERVAL -1 DAY` raises the boundary). A full query that carries a fixed old-date lower bound (the full-build branch of a watermark variable) is accepted: the bound keeps every row.
  * **R3, key de-duplication.** R1 or R2 plus `QUALIFY ROW_NUMBER() OVER (PARTITION BY <source key> ...) = 1`; re-delivered rows (`duplicate`) are then absorbed.
  * **R4, truncated watermark.** R2 whose watermark is `DATE(ts)`, `TIMESTAMP_TRUNC(ts, ...)` or `CAST(ts AS DATE)` `>=` the `MAX` of the same expression projected by the model, so a run reloads the newest day. A strict `>` is refused.
  * **R5, group re-aggregation.** `SELECT g, <SUM/COUNT/MIN/MAX/AVG>, MAX(ts) AS m ... GROUP BY g` merged on `g`, whose incremental run keeps the groups with a row at or after the watermark on `m` (`g IN (SELECT g FROM source WHERE ts >= ...)`). Inserts only; no `HAVING`. Assumes `g` is never NULL (the change kinds only make NULLs through `null_key`, which is refused).
  * **R6, unchanged joined tables.** R1 over a fact table joined (`INNER` or `LEFT`, fact table first) to tables the contract's `tables` never changes.
  * **R7, merge of a full re-run.** The incremental query is the full query (once always-true filters such as `WHERE 1 = 1` and `SELECT * FROM (q)` wrappers are removed) and the run merges it on `uniqueKey`, with no `updatePartitionFilter` and no `pre_operations` left after substitution. Then every run replaces each table row by the query's current row for the same key, provided three conditions hold in every source state the contract reaches:
    1. `uniqueKey` is unique and non-NULL in the query's output. This is read with `output_properties.infer_properties` under the key facts the contract preserves (`duplicate` makes a source key non-unique, `null_key` removes both facts, other columns get no NOT NULL fact), or from a `UNION ALL` whose branches each carry a distinct constant in a key column. A `ROW_NUMBER() ... = 1` de-duplication is read as a `GROUP BY` on its partition, for this analysis only.
    2. No key ever leaves the output. `kumosql.incremental_monotone.analyze` classifies each source as frozen, growing or keyed under the contract and follows positive filters, `INNER` and `LEFT` joins, `GROUP BY`, `QUALIFY` de-duplication, `UNION`, `EXCEPT` against a frozen query and a `HAVING` with a monotone `COUNT`, `MAX` or `MIN`; `NOT`, `NOT IN`, `LIMIT`, window values and aggregates are not stable. A merge never deletes, so a key that could leave the result would stay behind.
    3. The query does not depend on tie-breaking or chance (`kumosql.incremental_ties.tie_reasons` finds nothing).

    The theorem and its proof by induction on runs are in the `kumosql.incremental_merge` docstring.
* **diverges**: a random search over change sequences allowed by the contract found one on which the model differs from a full refresh (or fails), shrunk by dropping batches and statements while it still diverges. It is returned with the verdict and replays on its own.
* **nondeterministic**: the full refresh itself depends on how the engine breaks ties, so there is no single table for the incremental one to equal. A model with an order-sensitive construct (`ROW_NUMBER`, `FIRST_VALUE`/`LAST_VALUE`/`NTH_VALUE`, `LAG`/`LEAD`, `NTILE`, `ROWS` frames, `ANY_VALUE`, an unordered `ARRAY_AGG` or `STRING_AGG`, `LIMIT`, `RAND`) whose partition and ordering columns do not determine the row gets a tie witness: a reachable source state on which the full query, evaluated on the rows as loaded and again on the same rows stored in reverse, gives different results (DuckDB breaks ties by input order). Only a witness produces this verdict; without one the model goes on to the divergence search.
* **unknown**: no proof rule applies and the search found nothing. This is bounded evidence, not a proof.

### Assumptions

A `safe` verdict holds under these, each stated in the rule docstrings:

* **The contract.** There is no contract-free correctness. A change kind outside the contract can break a proved model.
* **Declared source keys.** A key in `SourceTable.key` is unique and non-NULL until `duplicate` or `null_key` is allowed.
* **The default precedes every event time.** The `COALESCE` default of a watermark is assumed to be older than every event time (R1 to R6), which is why it must be a literal date no later than 2000.
* **Group columns are not NULL** for R5.
* **Whole runs, one at a time.** Each run sees one consistent snapshot of the sources, and runs do not overlap; the table is only written by its own runs.
* **Pinned clock.** `CURRENT_TIMESTAMP` is one value per run, and only an ignored audit column may use it in a proved model.
* **Dataform's templates** are as read in 3.0.62, and BigQuery's `MERGE` raises the one-target-row-many-source-rows error.

### Not decided

* A model whose `pre_operations` write the table it reads (a delete-then-reload window) is decided only by the search: no proof rule covers it, so the safe ones stay `unknown`.
* A model with a non-key column that can be NULL and is used as a group or key is only refuted when the generator produces a NULL there, which it does not for ordinary columns.
* JavaScript helpers in the query or in `pre_operations` stay `unsupported`.

<!-- Sections for later changes go here, each under its own heading: R8 (reload windows), repairs, change-generator gaps. Not written yet. -->

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

### Merge of a full re-run, script variables, neutral operations and ties (dev split)

The corpus also holds `tests/fixtures/incremental/full_rerun_cases.json`: 49 dev cases written from SQL semantics before any rule for them existed (24 safe, 24 diverges, 1 nondeterministic), covering merges of a full re-run, script variables, data-neutral pre- and post-operations and ties, with a near miss for each condition of R7. A second file, `full_rerun_heldout.json`, holds 16 blind cases (8 safe, 8 diverges) written by a separate author; it is run once, at the end, and never used to tune a rule, so no held-out number for it is reported on this page yet.

| Dev split, 49 new cases | proven safe | refuted | nondeterministic | unknown | wrong |
| --- | --- | --- | --- | --- | --- |
| Baseline, before the new rules (the simulator already handled the cases) | 0 | 19 of 24 diverging | 0 | the rest | 0 |
| Now | 23 of 24 safe | 23 of 24 diverging | 1 of 1 | 2 | 0 |

The baseline is the point of the work: a merge of a full re-run had no proof rule, so none of its safe cases could be proven and 5 of its diverging ones were not found. The two unknowns are `pre-operation-reload-own-window` (safe; it deletes and reloads a window, which no rule covers) and `full-rerun-group-nullable-key` (diverges; the change generator never puts NULL in a non-key column, so it cannot reach the NULL group key; leaving it unknown is correct, not a wrong answer). Whole dev split, boundary cases included, at the time of writing: 83 of 86 decided (35 proven, 47 refuted, 1 nondeterministic), 3 unknown, 0 wrong, 0 fidelity failures. The recorded scores are in `benchmarks/results/incremental-*.json`, which the scoreboard reads, and are authoritative; these dev numbers are optimistic because the rules were written against these cases. "0 wrong" means no definite answer differs from its label, and no counterexample or tie witness fails to replay.

**How cases are labelled.** Each case is authored with a label for its contract and a scripted change sequence. A fidelity check replays the script in the simulator and requires the outcome to match the label (this caught two authoring mistakes in the blind set). Detection never sees the script. Authored cases are original; adapted material (pg_ivm, the fixture repository) is recorded separately when added. No existing eval covers incremental maintenance (the SQLSolver, R-Bot, SQL-IQ and rewriting evals compare queries, not run histories); `synthetic_check` checks rewrites on random data and is the nearest related code.

**Caveats.** The proof rules and the change generator were developed on the 37 dev cases, so the dev numbers are optimistic; the held-out set is small. Every failure found is kept as a case.

## pg_ivm workloads

`python tools/incremental_pgivm.py <pg_ivm checkout>` reads pg_ivm's `sql/pg_ivm.sql` and `sql/outer_join.sql` (v1.16, commit 22b4b45, PostgreSQL License) and writes `tests/fixtures/incremental/pgivm_cases.json`. Each case keeps the **original** workload (view query, statements before and after it, verbatim Postgres SQL, file and line) apart from the **adapted** one (DuckDB statements and the Dataform model). Savepoint rollbacks start a new workload from the same base state; exact duplicates are dropped.

pg_ivm maintains views with its own engine, which Dataform lacks. The adapted model is the common Dataform append pattern, the same for every workload and fixed before running: every source table gains a `_loaded_at` load time (insertion order), the model projects the first table's `_loaded_at` (its `MAX` for an aggregate) and each incremental run reads only rows past `MAX(__loaded_at)` of its own table. Inserts into the first table keep it correct; updates, deletes, changes to joined tables and re-aggregation break it.

`python tools/incremental_bench.py --track pgivm`: 1,069 workloads extracted, 336 after de-duplication, **110 adapted**, 226 unsupported (216 use data-modifying CTEs, which apply several changes in one snapshot and cannot be replayed one by one; 10 use CTEs, subqueries, `DISTINCT` or non-table sources). Of the 110: **100 drift on their script and all 100 are refuted again by `check_incremental`, without the script**, 8 agree with their script (not scored: no label), 2 could not be run. The first run, at 20 seeds, refuted 95 and left 5 unknown; the seed count was then raised to 30 (the default), which refutes all 100. **That 100/100 is tuned on test; the clean number is 95/100.** No proof was given for a drifting workload. Held-out: `--track pgivm-heldout` gives each of the 29 distinct adapted views one fresh random insert/update/delete script (generator seed 9001, never seen while tuning) and scores the drifting ones once at the frozen 30 seeds: 28 of 29 drift and all 28 are refuted, 0 wrong.

## Scanning a project

`python -m kumosql incremental-report PROJECT` (`kumosql-incremental-report`) checks every `type: "incremental"` action under `definitions/` against three contracts: `append_only`, `late_and_duplicate` and `mutable` (updates and deletes). Source columns are inferred from what the query reads (`id` taken as the unique key, `*_at` / `ts` as event time), so each answer is about the model on those assumed sources; `--source-schema` supplies real columns. Models it cannot simulate (JavaScript helpers in the query, `pre_operations` that run on every run, unparsable SQL) are listed as unsupported, never guessed.

On the generated fixture project (`python tools/make_dataform_fixture.py OUT --models 600 --seed 11`): 62 incremental models, 58 simulated and 4 unsupported (3 use a JavaScript date helper). Each re-runs its full query and merges on `id`, and all but one set `bigquery.updatePartitionFilter` relative to `CURRENT_DATE`, so R7 does not apply to them (it refuses an `updatePartitionFilter`). Under each of the three contracts 33 diverge and 25 are unknown. The 33 diverge even under `append_only`: a one-row source whose row is older than the filter window, followed by a run with no change, leaves a second copy of the row, because the `MERGE` cannot match it. The `GRANT` in each model's `post_operations` is data-neutral, so these models are simulated; before data-neutral statements were recognised the whole fixture was `unsupported` because of them. This is a scan, not a scored eval: the sources are inferred, so there are no labels. The scan reports `safe`, `diverges`, `unknown`, `unsupported` and `timeout`; the counts are aggregates for orientation, not a score, and are not in the scoreboard.

On the real fixture repository (`kumosql-dataform-fixture`, commit f056260, read-only): 316 incremental models, 267 simulated and 49 unsupported (35 always-run `pre_operations`, 13 JavaScript `dateFilter` helpers, 1 unparsable). `append_only`: 265 unknown, 2 timeout (no proof rule covers the fixture's merge-of-a-full-re-run definition). `late_and_duplicate`: 105 diverge (a re-delivered row makes the `MERGE` match one target row twice), 162 unknown. `mutable`: 206 diverge (a deleted or changed source row leaves its old row), 61 unknown. Run took about 17 minutes. Unscored, for the same reason.

Known limit: the real-repository counts above were recorded before R7, the tie witness, script variables and data-neutral operations existed. They are not rerun here (the run takes about 17 minutes), so they describe the older code.
