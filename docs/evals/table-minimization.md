# Table minimization eval

[Plain-language version](../../docs_simple/evals/table-minimization.md)

Given a pipeline of tables (say tables 1 to 20) and the tables that must stay, how simple can the pipeline get? A **protected** table must still exist under the same name and give exactly the same output: the same column names in the same order and the same bag of rows, on every database. Every other table may be dropped, merged, inlined or rewritten. The answer is scored by complexity: the repo's sqlfluff score summed over the pipeline, plus one per table.

```
python tools/minimization_bench.py                     # dev split, KumoSQL's table minimizer
python tools/minimization_bench.py --minimizer refactor   # the Refactor search, for comparison
python tools/minimization_bench.py --minimizer reference   # the stored references (sanity check)
python tools/minimization_bench.py --split held_out    # final evaluation only
python tools/minimization_bench.py --cases sourced     # cases adapted from public projects
python tools/minimization_bench.py --verify benchmarks/table_minimization/generated.jsonl
python tools/make_minimization_cases.py --jobs 4       # rewrites generated.jsonl (verifies every case)
```

No LLM runs at evaluation time. The cases were written once with Claude (a Python generator plus hand-written cases), verified on DuckDB and committed.

## Source and overlap

- Source: written for this eval, so there is no upstream version to pin. `generated.jsonl` comes from `tools/make_minimization_cases.py` (seed 20261002); `handwritten.jsonl` from `tools/minimization_handwritten.py`. Cases adapted from public projects go in their own `sourced-<name>.jsonl` files with `source` set, and are reported separately.
- Overlap: the [whole-pipeline equivalence eval](pipeline-equivalence.md) asks whether a *given* refactor keeps every output; this eval asks a minimizer to *find* the refactor. The duplicate-detection eval scores finding repeated SELECTs, not removing them.
- Held out: one case in five, chosen by a hash of the case id (`minimization_cases.held_out_split`) when the case is written. Nobody tunes on them; they are run once, at the end.

## Case format

One case per line in `benchmarks/table_minimization/*.jsonl`; the harness reads every file there.

| Field | Meaning |
| --- | --- |
| `id` | Unique across all files (`gen-NNNN`, `hw-NNNN`, ...). |
| `source` | `generated`, `handwritten`, or the public source a case was adapted from. |
| `families` | Redundancy patterns present (table below). |
| `split` | `dev` or `held_out`, fixed when the case is written. |
| `dialect` | Dialect of every SQL string (`bigquery`). Every query also runs on DuckDB after `sqlglot.transpile(sql, read=dialect, write="duckdb")`. |
| `sources` | Input tables: `columns` (name to type, in order: `INT64`, `STRING`, `FLOAT64`, `NUMERIC`, `BOOL`, `DATE`, `TIMESTAMP`), optional `key` (unique, NOT NULL), `not_null` and `values` (what random databases draw from; every literal in the case is added). |
| `tables` | The pipeline: table name to one SELECT, naming sources and tables by bare name. |
| `protected` | Tables that must exist in the output with identical output. |
| `original.complexity` | `{"score", "structural", "tables"}` of `tables`. |
| `reference.tables`, `reference.complexity` | A known simpler pipeline, verified equal on every protected table. A target, not necessarily the optimum. For a case with nothing to remove it equals `tables`. |
| `reference_kind` | Optional: `generated` (default), `external` (the source's own simpler version), `mechanical` (unread and pass-through tables removed) or `hand` (simplified by hand offline, then checked like any reference). Quality is reported per kind. |
| `traps` | Tempting simplifications that change a protected output: `note`, full pipeline `tables`, the protected tables it `changes`, and a `witness` (source rows on which it differs). |
| `data` | Optional real rows, loaded as one more check database. |
| `verification` | How the reference was checked: DuckDB (optimizer off) on `databases` databases from `seed`, and per protected table whether KumoSQL proved it (`proved`). |

Complexity is `kumosql.formatting.pipeline_complexity(tables)`: `kumosql.formatting.complexity` (sqlfluff structural score: joins 2, CTEs 1, subqueries 3, set operations 2, CASE 1, window functions 2, AND/OR 0.5, nesting 2) summed over the tables, plus 1 per table. The per-table term is what makes dropping a pass-through table count, since a bare `SELECT *` scores 0. Lower is better; between equal scores, shorter SQL wins.

A minimizer is a Python callable `minimize(case_input) -> dict[str, str]`; `case_input` has `id`, `dialect`, `sources`, `tables` and `protected` (no reference, no traps).

## Families

| Family | Redundancy | Traps |
| --- | --- | --- |
| `passthrough_chain` | Chains of 1-4 `SELECT *` or renaming views between a source and 1-3 reports; a middle link may be protected | Dropping the chain together with a filter one link applies |
| `duplicated_logic` | 2-4 copies of the same logic, spelled differently (aliases, `'paid' = status`, `COUNT(1)`, `GROUP BY 1`); a copy may be protected | Merging a near-copy (`COUNT(amount)` for `COUNT(*)`, `LEFT JOIN` for `JOIN`, `<> 'food'` for `= 'toys'`) |
| `dead_tables` | 1-7 tables no protected table reads, some reading each other | Dropping a table that looks unused but feeds a protected `IN` filter |
| `unused_columns_joins` | A wide table with CASE and window columns and a joined column nobody reads; a `LEFT JOIN` on the other table's key | An inner join, or a join to a non-key column that repeats rows, removed |
| `cte_repeats_table` | A CTE that repeats another table's query | Replacing a CTE with a table it only resembles |
| `mergeable_tables` | Single-reader filter stages; per-customer aggregates joined back together; per-region tables put back together with `UNION ALL` or `UNION DISTINCT` | Losing a stage filter; keeping the NULL group the joins dropped; widening the region filter; dropping DISTINCT |
| `redundant_filters` | A filter implied by an upstream one (`amount > 0` after `amount > 3`) | Keeping the implied filter and dropping the one that implies it |
| `irreducible` | Nothing to remove | `amount > 0 OR amount <= 0` dropped (NULLs), `COUNT(col)` to `COUNT(*)`, DISTINCT dropped, joins removed, HAVING dropped, `status = status` dropped, `NOT IN` dropped, `UNION DISTINCT` to one filter |

A generated case composes one to several modules of these families (each family leads in turn), with 3-5, 6-10, 11-15 or 16-20 tables, over e-commerce sources (orders, customers, items, products, payments, events) with declared keys.

## How a case is verified

Before a case is written, the reference must give every protected table the same column names and bag of rows as the original on 300 random databases (0-8 rows per table, NULLs, duplicates, keys and NOT NULL respected, values from small domains plus every literal), the targeted ones (empty, all-NULL, duplicated rows) and every trap witness, on DuckDB with the optimizer off. Each trap must differ on some database; that database is shrunk row by row and stored as the trap's witness. The reference must score lower than the original unless the case has nothing to remove. KumoSQL's `prove_models` is then tried on every protected table and the outcome is recorded; a reference the prover cannot prove is still admitted on DuckDB agreement, and the case records it as not proved.

## What is scored

The harness gives the minimizer the pipeline, then checks its output: every protected table must exist under its name and agree with the original on the targeted databases, 100 random ones, the case's `data` and every trap witness. Outcomes, kept as separate numbers:

- **Correctness**: `proved` (every protected table that changed is proved equal by `prove_models`), `agreed` (equal on every database, not proved), `same` (each protected table and everything it reads unchanged) or `wrong` (a protected table missing, renamed or reordered columns, an output that does not run, or a difference on any database, trap witnesses included). Wrong must be 0. A minimizer that raises is an `error`.
- **Coverage**: the share of cases whose output scores lower than the original and is not wrong.
- **Quality**: per case `(original - output) / (original - reference)`, wrong counted as 0, averaged over the cases whose reference is simpler than the original. 1 matches the reference, above 1 beats it.
- **Runtime**: seconds per case (median, max) and in total, minimizer only.

Results: `benchmarks/results/table-minimization.json` (generated and hand-written cases) and `benchmarks/results/table-minimization-sourced.json` (adapted cases, below).

## Results

Cases: 334 (300 generated, 34 hand-written), 270 dev and 64 held out, 3 to 20 tables each, 711 traps. Every reference agrees with its original on DuckDB; KumoSQL's prover proves 170 of the 334 references in full (the rest are admitted on DuckDB agreement; the most common gaps are removing a LEFT JOIN on a declared key and folding joined per-customer aggregates).

KumoSQL's table minimizer (`kumosql.table_minimizer`, see [table minimization](../table-minimization.md); 60 s search limit per case), measured 2026-10-04 on master after #398 and the complexity score change (comma-joined FROM items count as joins, `IF()` as a CASE; the stored complexities were rescored, and the first measurement is in the table's history on 2026-10-03). It was written without looking at the cases, and the held-out split was run once:

| Split | Cases | Wrong | Proved | Agreed | Same | Improved | Quality | Beats reference | Complexity (original / output / reference) | Median / max seconds |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | --- | --- |
| dev | 270 | 0 | 249 | 4 | 17 | 255 (94.4%) | 0.81 | 51 | 4,831.5 / 3,164.5 / 2,693.5 | 3.4 / 66 |
| held out (run once) | 64 | 0 | 60 | 2 | 2 | 63 (98.4%) | 0.75 | 11 | 1,084.5 / 739.0 / 592.5 | 3.2 / 64 |

By family on dev (cases improved / cases, quality): dead tables 94/94, 0.85; duplicated logic 78/78, 0.98; unused columns and joins 80/80, 0.77; pass-through chains 92/93, 0.81; redundant filters 69/70, 0.75; CTE repeats a table 73/80, 0.71; mergeable tables 94/104, 0.57; cases with irreducible tables 51/59, 0.75. "Agreed" outputs were proved by the minimizer's own check (inlined down to the tables both sides share) but not by the harness's `prove_models`. The weakest family is mergeable tables: folds of joined per-key aggregates and LEFT JOINs that the prover cannot prove are rejected. The minimizer does not add shared tables, so a reference that factors common logic into a new table can stay ahead.

`tests/test_minimization_bench.py` holds floors for both minimizers on every other dev case of 6 to 8 tables (27 cases): the table minimizer improves 23 and proves 22 there.

For comparison, the Refactor search (`--minimizer refactor`, `kumosql.refactor.search`, protected tables protected and every other table editable, 60 s per case), measured 2026-10-04:

| Split | Cases | Wrong | Proved / unchanged | Improved | Quality | Complexity (original / output / reference) | Median / max seconds |
| --- | ---: | ---: | ---: | ---: | ---: | --- | --- |
| dev | 270 | 0 | 88 / 182 | 164 (60.7%) | 0.39 | 4,831.5 / 3,848.5 / 2,693.5 | 2.4 / 68 |
| held out (run once) | 64 | 0 | 11 / 53 | 23 (35.9%) | 0.24 | 1,084.5 / 923.5 / 592.5 | 2.5 / 53 |

"Unchanged" means each protected table and everything it reads kept its SQL (dropping dead tables leaves the protected ones untouched). Until 2026-10-03 the harness re-rendered every table of the search's output through sqlglot, so untouched tables were counted as changed and then proved (269 and 63 proved); it now keeps the case's own SQL for any table the search did not change, which also stops a comma join from coming back as a scored `CROSS JOIN`. Improvement and quality did not move.

By family on dev (cases improved / cases, quality): dead tables 94/94, 0.57; duplicated logic 77/78, 0.72; unused columns and joins 59/80, 0.33; mergeable tables 66/104, 0.24; redundant filters 44/70, 0.32; CTE repeats a table 36/80, 0.28; pass-through chains 40/93, 0.14; cases with irreducible tables 42/59, 0.40. The search beats the reference on 5 cases. It drops dead tables and merges duplicates well; it cannot rewrite SQL inside a table (no filter folding, no join removal, no CTE replacement), and inlining a table into its readers adds a derived table, which scores higher, so pass-through chains mostly stay. The stored references, run through the same checker, score 322/322 improvable cases at quality 1.0 with 0 wrong.

## Sourced cases

No public benchmark poses this task, so cases were adapted from projects that come close. They are in `sourced-<name>.jsonl`, scored apart (`--cases sourced`) and never mixed into the generated numbers. Licences are in `benchmarks/table_minimization/licenses/`.

| File | Source (pinned) | Cases (dev / held out) | Tables | Protected | Reference |
| --- | --- | --- | --- | --- | --- |
| `sourced-sqlglot.jsonl` | sqlglot optimizer fixtures `merge_subqueries`, `eliminate_ctes`, `eliminate_joins` (MIT, commit `bffcdefc16b0`) | 76 (55 / 21) | 2-4 | `result` | `external`: sqlglot's own simplified query, as one table; 23 of the 76 are `hand` where a simpler pipeline was found |
| `sourced-fivetran.jsonl` | Fivetran dbt packages asana, intercom, iterable, klaviyo, mailchimp, recurly, github (Apache-2.0, commits in `source`) | 49 (43 / 6) | 18-49 | the end models (all of them, or one) | `mechanical`: unread and pass-through tables removed; 10 of the 49 are `hand` |
| `sourced-jaffle.jsonl` | dbt-labs/jaffle_shop_duckdb (Apache-2.0, commit `20cc9043`) | 3 (2 / 1) | 5 | `customers`, `orders` | `hand` |

- **sqlglot** (`tools/make_sqlglot_minimization_cases.py SQLGLOT_CLONE`): every CTE and FROM/JOIN derived table of a fixture's input becomes its own table and the outer query the protected `result`. A pair is kept only if the unsplit input, the split pipeline and sqlglot's output agree on 60 random DuckDB databases; 34 of 110 pairs were left out (LIMIT or windows, sources outside the plain test schema, options, or SQL that does not round-trip). Where sqlglot's output is not simpler by this score (it keeps outer-join subqueries) the reference is the pipeline itself. Traps are one-step mutants of the reference (a filter conjunct dropped, an outer join made inner, DISTINCT dropped) with the random database that shows the difference.
- **Fivetran and jaffle_shop** (`tools/make_fivetran_minimization_cases.py --clones DIR --work DIR`, needs dbt-core and dbt-duckdb, which KumoSQL does not depend on): each package's integration-test project is built on DuckDB with every model as a table, and the compiled SQL is made bare-named, transpiled to BigQuery and checked by running it back on DuckDB against the tables dbt built. Adaptations, listed in each case's `note`: `current_timestamp` and `current_date` fixed to 2026-01-01; `STRING_AGG`/`ARRAY_AGG` without `ORDER BY` ordered on their own argument; a CTE named like the table it reads renamed to `<name>__cte`. The seed rows are the case's `data`, and STRING columns take their `values` from them. Models that fail the round trip are dropped with everything downstream (10 of recurly's 49, 1 of github's 43). The reference is verified on the seed data and at least 50 random databases.
- **Checking real pipelines**: a case with `data` has sources whose values are narrower than their types (JSON or timestamps stored as text), so a database on which the original itself fails is skipped for that table rather than counted (`minimization_cases.compare(skip_errors=True)`); `--verify` reports a protected table no database checked. The harness runs DuckDB on one thread so that the row-order check is repeatable. `tools/minimization_isolated.py` runs each case in its own process with a memory cap and a time limit (default 5 GB and 300 s), which the large Fivetran pipelines need; a case that exceeds either counts as an error, not as improved and never as wrong.
- **Overlap**: none of these sources is used by another eval. Spider 2.0's dbt projects use the same Fivetran packages, but their data is not reachable here ([Spider 2.0](spider2-bench.md)).
- **Checked and not used**: dbt Labs' ADE-bench (a few tasks are exactly "inline this model, output unchanged"; its DuckDB files are 100 MB+ release downloads), dbt-project-evaluator (stub models without data), ELT-Bench (no gold SQL, data on Google Drive and HuggingFace), the dbt "refactoring for modularity" course repos (no licence), GoogleCloudPlatform/security-analytics (raw and alert views differ by a time window and read nested log records), sqlcheck (single queries, no rewrites). No multi-query optimization or pipeline-deduplication workload with released SQL was found.

KumoSQL's table minimizer on the adapted cases, measured 2026-10-04 with the current complexity score (each case in its own process with a 5 GB memory cap and a 300 s limit, `python tools/minimization_isolated.py --cases sourced`; a run that hits a limit counts as an error: not improved, never wrong):

| Cases | Split | Wrong | Proved / agreed / unchanged | Improved | Quality | Limit hit | Complexity (original / output / reference) |
| --- | --- | ---: | --- | ---: | ---: | ---: | --- |
| sqlglot | dev, 55 | 0 | 43 / 0 / 12 | 38 | 0.70 | 0 | 198.5 / 141.0 / 107.0 |
| jaffle_shop | dev, 2 | 0 | 0 / 2 / 0 | 2 | 0.41 | 0 | 66.0 / 46.0 / 19.0 |
| Fivetran | dev, 43 | 0 | 0 / 2 / 35 | 18 | 0.29 | 6 (300 s) | 11,717.5 / 9,935.0 / 5,826.0 |
| all | dev, 100 | 0 | 43 / 4 / 47 | 58 | 0.52 | 6 | 11,982.0 / 10,122.0 / 5,952.0 |
| all | held out, 28 (run once) | 0 | 20 / 0 / 8 | 15 | 0.52 | 0 | 1,650.0 / 1,606.5 / 599.0 |

Quality by reference kind (dev): sqlglot's own output 0.91, hand-simplified 0.33, mechanical 0.26. On the held-out split the minimizer improved 14 of 21 sqlglot cases and the jaffle_shop case and none of the 6 Fivetran pipelines. The minimizer never beats a reference here. The hand references hold rewrites that need a proof rule it does not have: joins that can never match (sides filtered to different constants, then joined), a LEFT JOIN to a DISTINCT table whose columns are unread, GROUP BY on a constant column, NOT EXISTS against a one-row literal table, and `SUM(DISTINCT b) * COUNT(*)` for an aggregate over a cross join. On Fivetran the protected end models are large, and the greedy search either leaves them alone (47 runs ended with the protected tables unchanged, which includes dropping unread tables) or hits the 300 s limit; asana-all, asana__daily_metrics and four github models did. "Agreed" outputs (jaffle_shop and two intercom models) were checked on every database but not proved by `prove_models`.

Earlier numbers for these cases (Refactor search, 33 of 100) were measured under the previous complexity score and references and are not comparable; the Refactor search was not rerun on the adapted cases.

### Hand-simplified references

36 of the 128 references are `reference_kind: "hand"`: 23 sqlglot, 3 jaffle_shop, 6 klaviyo and 4 recurly cases. Each candidate was accepted only if it was strictly simpler than the stored reference under the current score, passed `verify_cases` (200 DuckDB databases, the seed rows and every trap witness) and the harness's check (a proof where KumoSQL can, otherwise agreement on every database). The other Fivetran references (asana, intercom, iterable, mailchimp, github and the remaining klaviyo and recurly models) are still mechanical, so they are weaker targets than the hand ones. Take the quality figures as "how much of that target the minimizer reaches", not as an optimum.

## Limits

- Agreement on DuckDB databases is evidence, not proof; a reference is admitted on it. Small domains make NULLs, duplicates and key collisions frequent, and every trap is caught, but an untried database could still separate a pair.
- Column types are not compared (as in the provers): `SUM` of an `INT64` column is an integer either way, but a rewrite that only changes a type is not caught.
- DuckDB stands in for BigQuery, so a function the two treat differently can mislead the check. sqlglot turns `COUNTIF` into DuckDB's `count_if`, which returns NULL rather than 0 when every input is NULL; the cases use `SUM(IF(..., 1, 0))` instead.
- Where an original query depends on row order (a window ordered on a non-unique column, `LIMIT`, `ARRAY_AGG`, `ANY_VALUE`), a database counts for a protected table only if the original gives that table the same output with every source's rows reversed.
- Table references are bare names; the harness's check treats a CTE with the same name as a table as shadowing it everywhere in that query. The minimizer itself scopes a `WITH` table to its own query and keeps names case-sensitive (see [Table minimization](../table-minimization.md#names-and-scripts)); the cases hold no names that differ only by case.
- Only the pipeline's protected tables are observed. Quality rewards the reduction of the whole pipeline, so removing an unprotected table that something outside the modeled pipeline reads (a dashboard, another job) scores as a full reduction while breaking that reader. Read quality as "structural reduction with the protected set kept", not as safe to deploy; to keep an outside reader's table, add it to `protected`.
- Generated SQL is templated: it covers the eight patterns at sizes up to 20 tables, not the variety of real projects. The hand-written and sourced files add that.
