# Table minimization eval

[Plain-language version](../../docs_simple/evals/table-minimization.md)

Given a pipeline of tables (say tables 1 to 20) and the tables that must stay, how simple can the pipeline get? A **protected** table must still exist under the same name and give exactly the same output: the same column names in the same order and the same bag of rows, on every database. Every other table may be dropped, merged, inlined or rewritten. The answer is scored by complexity: the repo's sqlfluff score summed over the pipeline, plus one per table.

```
python tools/minimization_bench.py                     # dev split, KumoSQL's table minimizer
python tools/minimization_bench.py --minimizer refactor   # the Refactor search, for comparison
python tools/minimization_bench.py --minimizer reference   # the stored references (sanity check)
python tools/minimization_bench.py --split held_out    # final evaluation only
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
| `reference_kind` | Optional: `generated` (default), `external` (the source's own simpler version) or `mechanical`. Quality is reported per kind. |
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

Results: `benchmarks/results/table-minimization.json`.

## Results

Cases: 334 (300 generated, 34 hand-written), 270 dev and 64 held out, 3 to 20 tables each, 711 traps. Every reference agrees with its original on DuckDB; KumoSQL's prover proves 170 of the 334 references in full (the rest are admitted on DuckDB agreement; the most common gaps are removing a LEFT JOIN on a declared key and folding joined per-customer aggregates).

KumoSQL's table minimizer (`kumosql.table_minimizer`, see [table minimization](../table-minimization.md); 60 s search limit per case), measured 2026-10-03 on master after #398. It was written without looking at the cases, and the held-out split was run once:

| Split | Cases | Wrong | Proved | Agreed | Same | Improved | Quality | Beats reference | Complexity (original / output / reference) | Median / max seconds |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | --- | --- |
| dev | 270 | 0 | 249 | 4 | 17 | 255 (94.4%) | 0.80 | 51 | 4,831.5 / 3,164.5 / 2,682.5 | 4.6 / 74 |
| held out (run once) | 64 | 0 | 60 | 2 | 2 | 63 (98.4%) | 0.75 | 11 | 1,083.5 / 739.0 / 589.5 | 4.3 / 63 |

By family on dev (cases improved / cases, quality): dead tables 94/94, 0.84; duplicated logic 78/78, 0.98; unused columns and joins 80/80, 0.77; pass-through chains 92/93, 0.81; redundant filters 69/70, 0.75; CTE repeats a table 73/80, 0.71; mergeable tables 94/104, 0.56; cases with irreducible tables 51/59, 0.75. "Agreed" outputs were proved by the minimizer's own check (inlined down to the tables both sides share) but not by the harness's `prove_models`. The weakest family is mergeable tables: folds of joined per-key aggregates and LEFT JOINs that the prover cannot prove are rejected. The minimizer does not add shared tables, so a reference that factors common logic into a new table can stay ahead.

`tests/test_minimization_bench.py` holds floors for both minimizers on every other dev case of 6 to 8 tables (27 cases): the table minimizer improves 23 and proves 22 there.

For comparison, the Refactor search (`--minimizer refactor`, `kumosql.refactor.search`, protected tables protected and every other table editable, 60 s per case), measured 2026-10-03:

| Split | Cases | Wrong | Proved | Improved | Quality | Complexity (original / output / reference) | Median / max seconds |
| --- | ---: | ---: | ---: | ---: | ---: | --- | --- |
| dev | 270 | 0 | 269 | 164 (60.7%) | 0.39 | 4,831.5 / 3,848.5 / 2,682.5 | 3.5 / 67 |
| held out (run once) | 64 | 0 | 63 | 23 (35.9%) | 0.24 | 1,083.5 / 922.5 / 589.5 | 3.7 / 54 |

By family on dev (cases improved / cases, quality): dead tables 94/94, 0.57; duplicated logic 77/78, 0.72; unused columns and joins 59/80, 0.33; mergeable tables 66/104, 0.24; redundant filters 44/70, 0.32; CTE repeats a table 36/80, 0.28; pass-through chains 40/93, 0.14; cases with irreducible tables 42/59, 0.40. The search beats the reference on 5 cases. It drops dead tables and merges duplicates well; it cannot rewrite SQL inside a table (no filter folding, no join removal, no CTE replacement), and inlining a table into its readers adds a derived table, which scores higher, so pass-through chains mostly stay. The stored references, run through the same checker, score 322/322 improvable cases at quality 1.0 with 0 wrong.

## Limits

- Agreement on DuckDB databases is evidence, not proof; a reference is admitted on it. Small domains make NULLs, duplicates and key collisions frequent, and every trap is caught, but an untried database could still separate a pair.
- Column types are not compared (as in the provers): `SUM` of an `INT64` column is an integer either way, but a rewrite that only changes a type is not caught.
- DuckDB stands in for BigQuery, so a function the two treat differently can mislead the check. sqlglot turns `COUNTIF` into DuckDB's `count_if`, which returns NULL rather than 0 when every input is NULL; the cases use `SUM(IF(..., 1, 0))` instead.
- Where an original query depends on row order (a window ordered on a non-unique column, `LIMIT`, `ARRAY_AGG`, `ANY_VALUE`), a database counts for a protected table only if the original gives that table the same output with every source's rows reversed.
- Table references are bare names; a CTE with the same name as a table shadows it everywhere in that query (scoping is not modelled further).
- Generated SQL is templated: it covers the eight patterns at sizes up to 20 tables, not the variety of real projects. The hand-written and sourced files add that.
