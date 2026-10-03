# Transformations on TPC-H, TPC-DS and the Join Order Benchmark

[Plain-language version](../../docs_simple/evals/transformation-bench.md)

`tools/transformation_bench.py` applies KumoSQL's transformations to three standard workloads and measures four things separately. A rewrite suite can preserve every result by changing nothing, so "nothing broke" is never the only number reported.

- **Correctness**: a rewrite KumoSQL marks *proven* that returns different rows from the original is wrong. Both queries run in DuckDB on the benchmark's real data, and on generated tables as well.
- **Coverage**: how many queries each transformation changed, and how many of the changes were proven or verified by execution.
- **Usefulness**: proven rewrites that DuckDB plans differently and that run at least 5% faster on real data. The plan must differ because formatting alone "speeds up" a third of the TPC-DS queries by more than 5%, which is timer noise.
- **Performance**: time to transform each query, prover time by the number of joined tables, and DuckDB runtimes.

The transformations are each registered cleanup rule on its own, plus the whole pipeline in its canonical order (the rules, then the formatter).

## Workloads and data

| Workload | Queries (pinned source) | Real data |
|---|---|---|
| TPC-H | the 22 official queries, one instance each (SQLStorm v0.0, `b3bb0b9`). Query 13 is left out because sqlglot cannot convert its column-list alias (`AS c_orders (c_custkey, c_count)`) to BigQuery. | scale 0.1 from `tpchgen-cli` |
| TPC-DS | the 99 templates as 102 statements, one instance each (SQLStorm v0.0) | scale 1 from DSB's `dsdgen` (`ec9a156`) |
| JOB | the 113 Join Order Benchmark queries ([gregrahn/join-order-benchmark](https://github.com/gregrahn/join-order-benchmark) `a396036`) | the IMDB snapshot (36M `cast_info` rows) from [jo-bench](https://github.com/danolivo/jo-bench) `ad516b3`, BSD-2 |

```
python tools/benchmark_corpora.py fetch sqlstorm tpch-data tpcds-data job-data
python tools/transformation_bench.py tpch tpcds job --json results.json
```

Fetching JOB's data clones 4.8 GB, builds a 2.8 GB DuckDB file and then deletes the CSVs.

## JOB: alternative forms

JOB tests qualification, dependency analysis, joins and prover scaling. Every query is a comma join of 4 to 17 tables, filtered and then aggregated with `MIN`. The tool writes five valid alternative forms of each query:

- `explicit_joins`: each join predicate moved into `JOIN ... ON`.
- `reversed_from`: the FROM list and the WHERE conjuncts in reverse order.
- `unqualified`: the table alias dropped from every column that only one table has (this needs the schema).
- `filter_ctes`: each table's own filters moved into a CTE that the query reads instead.
- `renamed_aliases`: every alias renamed.

It also writes two broken forms as negative controls. One drops a join predicate that the remaining equalities do not imply, and the other negates one single-table filter. The prover must prove every valid form and no broken one.

Each form is also run on IMDB and on generated tables. Column lineage must give every output column the same source columns in every form. It must also agree with the sources found by sqlglot's own qualification and scope analysis.

## Results

<!-- results:start -->
Measured 2026-10-02 with sqlglot 30.21. Synthetic checks use 3 seeds of 12 rows per table, and real timings are the median of 3 runs.

**Pipeline: 233/235 changed queries return the same rows on the real data, 0 wrong. 231 of the 235 changes are proven.** The other two are TPC-DS queries whose original fails in DuckDB on the real data. One of them agrees on generated tables, and the other cannot be run anywhere. One TPC-DS query is left unchanged. The 39 queries in the hash-held-out fifth score 38/39 on real data, 0 wrong.

| Rule | Workload | Changed | Proven | Same on real data | Plan changed | ≥5% faster |
|---|---|---:|---:|---:|---:|---:|
| `lift_subqueries` | TPC-H | 4/21 | 4 | 4 | 0 | 0 |
| | TPC-DS | 41/102 | 34 | 41 | 14 | 3 |
| `inline_single_use_ctes` | TPC-DS | 17/102 | 14 | 17 | 7 | 1 |
| `remove_redundant_parentheses` | TPC-DS | 17/102 | 16 | 17 | 0 | 0 |
| | JOB | 1/113 | 1 | 1 | 0 | 0 |
| `format_sql` | TPC-H | 21/21 | 21 | 21 | 0 | 0 |
| | TPC-DS | 101/102 | 101 | 99 | 0 | 0 |
| | JOB | 113/113 | 113 | 113 | 0 | 0 |
| pipeline | TPC-H | 21/21 | 21 | 21 | 0 | 0 |
| | TPC-DS | 101/102 | 97 | 99 | 7 | 1 |
| | JOB | 113/113 | 113 | 113 | 0 | 0 |

The other rules (`remove_trivial_predicates`, `deduplicate_ctes`, `remove_unused_ctes`, `remove_redundant_distinct`) change none of these queries, because the official queries have nothing for them to clean up. No rule crashed and no proven rewrite returned different rows on real or generated data. The pipeline's median speed-up is 1.01 on TPC-H, 1.01 on TPC-DS and 1.00 on JOB.

**Transformation time** per query (median, with the maximum in brackets): pipeline 1.1 s (1.9 s) on TPC-H, 2.2 s (26 s) on TPC-DS and 1.2 s (2.9 s) on JOB. Each structural rule takes a median of 7 to 29 ms. Nearly all the rest is the sqlfluff formatter.

**JOB alternative forms: 565/565 valid forms proven, 0/226 broken forms proven.**

| Form | Generated | Proven | Same on IMDB | Same lineage |
|---|---:|---:|---:|---:|
| `explicit_joins` | 113 | 113 | 113 | 113 |
| `reversed_from` | 113 | 113 | 113 | 113 |
| `unqualified` | 113 | 113 | 113 | 113 |
| `filter_ctes` | 113 | 113 | 113 | 113 |
| `renamed_aliases` | 113 | 113 | 113 | 113 |
| broken: `dropped_join_predicate` | 113 | 0 (32 refuted) | 51 (53 different, 9 timed out) | – |
| broken: `negated_filter` | 113 | 0 (32 refuted) | 11 (102 different) | – |

A broken form that agrees on IMDB is still a wrong rewrite. It only means this snapshot does not show the difference, which is why "not proven" is the correct answer for it. KumoSQL's column lineage for each of the 113 queries matches the sources found by sqlglot's qualified scope analysis, and every valid form keeps the same lineage. On the 16 held-out queries, 80/80 valid forms are proven and 0/32 broken forms are.

**Prover time by join size** (565 valid forms): median 0.05 s (max 0.2 s) for up to 5 tables, 0.11 s (0.5 s) for 6 to 9, and 0.30 s (4.4 s) for 10 to 17.

**What the harness got wrong first.** The first run reported one wrong rewrite (TPC-DS query 66). Its result is a `NaN`, and `NaN != NaN` in Python, so the row comparison failed. TPC-DS stream 0 also instantiates some templates twice, and the duplicate ids overwrote each other. Both are fixed in the harness, with tests in `tests/test_transformation_bench.py`. Neither involved a KumoSQL bug.
<!-- results:end -->

## Gaps

- The rules rarely make these queries faster in DuckDB. DuckDB already unnests subqueries and inlines CTEs, so lifting a subquery or inlining a CTE usually leaves its plan the same. A slower engine, or BigQuery's slot time, could show more gain, but that is not measured here.
- On TPC-DS, 4 pipeline rewrites give the same rows on real data but are not proven. Three inline a CTE that contains windows (queries 14, 17 and 82), and one drops parentheses inside a `CASE` over a `SELECT *` subquery (query 91). `lift_subqueries`, which the default pipeline does not run, leaves 7 more unproven. All of them count as unproven, not wrong.
- Generated tables are a weak check for JOB. With 12 rows per table, the 4- to 17-way joins with their filters are almost always empty, so even the broken forms "agree" there. On JOB, the proof and the IMDB run are the evidence.
- TPC-DS timings are at scale 1 and TPC-H at scale 0.1, so most queries take under 0.2 s and the differences are small.
