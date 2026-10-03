# LLM-R2 query sets: a scale test of the rewrites

[Plain-language version](../../docs_simple/evals/llmr2-bench.md)

`tools/llmr2_bench.py` runs KumoSQL's rewrites on every query of the [LLM-R2](https://github.com/DAMO-NLP-SG/LLM-R2) query sets (Li et al., VLDB 2025). Each changed query is executed against its original in DuckDB on the benchmark's real data. It repeats the [transformation benchmark](transformation-bench.md) at roughly fifty times the volume, and keeps LLM-R2's own train/test split.

LLM-R2 built these pools to learn which Calcite rewrite rules make a query faster in PostgreSQL. The latency labels need PostgreSQL and are not used here. Only the queries are used.

## What is measured

The measures are the same as the transformation benchmark's, reported separately:

- **Correctness**: a rewrite KumoSQL marks *proven* that returns different rows is wrong.
- **Coverage**: how many queries each transformation changed, how many of those changes were proven, and how many were verified by execution.
- **Usefulness**: proven rewrites that DuckDB plans differently and that run at least 5% faster (median of 3 runs).
- **Performance**: transformation time per query.

There are two transformations:

- **rules**: the cleanup rules in canonical order, without the formatter. The formatter only changes layout, and the transformation benchmark already measures it.
- **lift_subqueries**: run on its own, since the default pipeline does not include it.

Both queries run on TPC-H SF0.1, TPC-DS SF1 (DSB uses the TPC-DS schema; its data comes from DSB's own `dsdgen`) or the IMDB snapshot (for the synthetic JOB set). Results larger than 200,000 rows are compared with an order-independent hash. When the original does not run in DuckDB, the comparison falls back to generated tables.

## Source and splits

| Set | Train | Test | Notes |
|---|---:|---:|---|
| TPC-H | 3,772 | 500 | TPC-H templates with new constants. 4 train queries cannot be converted to BigQuery. |
| DSB | 1,635 | 500 | DSB templates. 17 test queries also appear in train. |
| JOB-syn | 4,446 | 500 | Synthetic select-project-join queries over IMDB |

The query files come from LLM-R2 at commit `91ba530`, `data/data_llmr2/queries`. The repository has no licence file, so `python tools/benchmark_corpora.py fetch llm-r2` downloads them at that pin, and they are never committed.

Every query is an original LLM-R2 query; none was adapted. None of the test queries matches a query of the transformation benchmark after normalisation, so the overlap with that eval is in the templates, not the text.

The **test files are the held-out split**. Development used only the train files, the implementation was not changed for this eval, and the test files were run once at the end.

```
python tools/benchmark_corpora.py fetch llm-r2 tpch-data tpcds-data job-data
python tools/llmr2_bench.py --split train --log train.jsonl
python tools/llmr2_bench.py --split test --log test.jsonl --json test.json
```

## Results

<!-- results:start -->
Measured 2026-10-02 with sqlglot 30.21.

**Test split, held out and run once: 225/225 changed queries are proven and return the same rows on the real data, 0 wrong.** The 225 queries account for 258 rewrites, and all of them are proven. 1,263 queries are left unchanged, 12 TPC-H queries cannot be converted to BigQuery, and nothing crashed or timed out. **Train split: 1,742/1,742 changed queries are proven and the same, 0 wrong** (9,853 queries, 4 not convertible).

| Split | Set | Queries | `rules` changed | `lift_subqueries` changed | Proven and same on real data | Wrong |
|---|---|---:|---:|---:|---:|---:|
| test | TPC-H | 488 of 500 | 0 | 110 | 110 | 0 |
| test | DSB | 500 | 74 | 74 | 148 | 0 |
| test | JOB-syn | 500 | 0 | 0 | – | 0 |
| train | TPC-H | 3,768 of 3,772 | 0 | 1,152 | 1,152 | 0 |
| train | DSB | 1,635 | 338 | 339 | 677 | 0 |
| train | JOB-syn | 4,446 | 0 | 0 | – | 0 |

Every changed query ran on the real data in both splits, so generated tables were never needed.

**Usefulness** (DSB, test split). Subquery lifting changed DuckDB's plan for 41 queries; 16 ran at least 5% faster and none more than 5% slower (median speed-up 1.03). The cleanup rules changed the plan for 72 queries; in a sample of 40 slowed train queries the rule was always CTE inlining. Only 1 of those ran at least 5% faster, and 50 ran more than 5% slower (median 0.89; these queries run in tens of milliseconds at SF1, so the loss is a few milliseconds each). The train split agrees: lifting gave 74 of 312 plan changes at least 5% faster with a median of 1.03, and inlining slowed 102 of 227 with a median of 0.965. Inlining is a readability rule, and it costs DuckDB a little. TPC-H's 1,262 lifted queries never changed DuckDB's plan, because DuckDB already decorrelates those subqueries.

**Performance.** Each query takes a median of 39 ms for the cleanup rules and 6 ms for lifting (max 0.6 s). The 9,853 train queries took about 7 minutes on 3 cores, DuckDB runs included.
<!-- results:end -->

## Notes

- JOB-syn queries are flat joins with simple filters, so no rule changes them. They test that the rewrites leave already-clean SQL alone.
- TPC-H's cleanup rules find nothing to clean in these queries. `lift_subqueries` rewrites the `IN`/`EXISTS` subqueries, which DuckDB already decorrelates, so the plan rarely changes.
- On DSB, lifting subqueries often gives a different DuckDB plan and sometimes a measurable speed-up. Inlining CTEs more often slows the query down slightly.
