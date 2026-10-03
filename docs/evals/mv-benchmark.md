# Materialized-view rewriting benchmark

[Plain-language version](../../docs_simple/evals/mv-benchmark.md)

Scores the python-forward part of the materialized-view benchmark from [edx-h/Benchmarking-MV-Based-Rewriting](https://github.com/edx-h/Benchmarking-MV-Based-Rewriting), commit `83da57fbc7` (arXiv 2607.19679): given a workload, which views does KumoSQL find, how many queries can it rewrite onto them, and is every rewrite proven. The paper's stages that need engines and data (Hive, Doris, StarRocks, latency, time savings) are out of scope.

```
python tools/mv_workload_bench.py                                  # JOB, all 113 queries
python tools/mv_workload_bench.py --all --sample 100 --json out.json   # all four workloads
python tools/mv_workload_bench.py --all --baseline --track mined   # the baseline
```

## Source and licence

The repository has no licence file (the paper states CC BY 4.0 for the artifacts). The workbooks and schemas are downloaded on first use into `$KUMOSQL_BENCH_DATA` (default `~/.cache/kumosql-bench/mv-benchmark`), checked against pinned SHA-256 digests and never stored in this repository. Workbooks are read with the standard library (`zipfile` and XML); `--data DIR` points at a local checkout instead.

| Workload | Queries in the workbook | Schema | Scored |
| --- | --- | --- | --- |
| JOB (`imdb_job.xlsx`, `raw_sql`) | 113 | IMDB, 21 tables | all 113 |
| SCALE (`scale.xlsx`) | 500 | IMDB | 100, picked by a hash of the query text |
| STATS (`stats.xlsx`) | 1,448 distinct | STATS, 8 tables | 100, same way |
| TPC-DS (`tpcds.xlsx`) | 938 | TPC-DS, 24 tables | 100, same way |

The repository ships no enumerated candidate views, only ten example views for JOB (`default_mv_list.xlsx`); its enumerators run against engines. The candidates here come from KumoSQL itself.

## Overlap with other evals

`tests/fixtures/mv_workload/overlap.json` (query text equal after lower-casing and removing whitespace, quotes and `::timestamp`):

* all 113 JOB queries are the JOB queries of `danolivo/jo-bench` that the join-order and transformation evals use;
* 136 of the 1,448 STATS queries are STATS-CEB queries (146 in the CEB file);
* SCALE and TPC-DS match nothing in the repository's other evals, and nothing matches the Calcite materialized-view cases (`docs/model-reuse.md`), which use other schemas.

What is new here is view enumeration and selection, which no other eval covers. Rewriting itself is the engine of `docs/model-reuse.md`.

## What is scored

* **Mined views** (`kumosql.view_candidates`): each query's join graph (tables, equalities between columns of different tables, closed under transitivity) is read; every join of two to four tables that at least two development queries share becomes a candidate, exposing every column of its tables that a query reads. Greedy selection keeps 12 views (10 on JOB) by joins saved across the workload. Views are mined from the development queries only.
* **Rewrites**: each query is rewritten over the largest chosen view it can use by `kumosql.model_reuse.rewrite_over_model`, which returns a rewrite only when the prover proves it equal to the query. Each rewrite is then re-run against the original on random DuckDB databases; a difference counts as **wrong**.
* **Given views** (JOB only): the ten views the benchmark ships, tried on every JOB query that joins all of a view's tables.
* **Poisoned views** (control): the chosen views with a condition no row meets. A query cannot be answered from an empty view, so every rewrite proven there would be wrong.
* **Baseline**: the existing prover alone, which can only say that a whole view equals a whole query.

Outcomes per query: `rewritten` (proven and verified), `no_rewrite` (a view applied, nothing proven), `no_view` (no chosen view joins tables the query joins), `unsupported`, `timeout`, `error`, `wrong`.

## Splits

JOB queries are held out by family (`1a`..`1d` are one family; one family in four, 17 queries). The other workloads are held out by a hash of the query text (one in four), so held-out queries are other instances of the same templates. Views are mined from development queries; the held-out queries are never read until they are rewritten. Nothing was tuned on held-out queries, but the engine's rules were developed while looking at JOB and STATS development queries.

## Results

Mined views, queries rewritten with a proof (every one verified on random databases, 0 wrong):

| Workload | Queries | Rewritten | Held out | No view applies | Unsupported | Other misses |
| --- | --- | --- | --- | --- | --- | --- |
| JOB | 113 | 108 | 12/17 | 5 | 0 | 0 |
| SCALE | 100 | 67 | 15/22 | 33 | 0 | 0 |
| STATS | 100 | 93 | 23/26 | 6 | 0 | 1 |
| TPC-DS | 100 | 86 | 19/26 | 5 | 9 | 0 |
| All | 413 | 354 | 69/91 | 49 | 9 | 1 |

The rewritten queries replace on average 3.6 joined tables by one view scan. Controls and baselines:

* poisoned views: 0 rewrites accepted of 355 queries tried (0 wrong);
* given views on JOB: 90 of 113 queries rewritten (13 of 17 held out), 21 join none of the views' tables, 2 not provable, 0 wrong;
* baseline (existing prover alone): 0 of 413 queries rewritten over the mined views (it applies to 355 of them), and 1 of 113 JOB queries over the given views. Only whole-query equality exists there, which is why the rewrite engine is needed.

`no_view` on SCALE is 26 single-table queries and 7 queries whose joins no other query shares. The nine TPC-DS `unsupported` queries have an `IN` the reuse engine does not read (a subquery or a non-literal list); literal `IN (...)` lists are read.

## Caveats

* The views are generalized joins that expose every column the workload reads, so a rewrite that uses a view derived from the same query family is easy; the held-out rows show how far views carry to other instances. Whether a view saves time is not measured (no engine or data).
* The random-database check is a second, bounded check: databases are small, so joins over many tables often return no rows.
* Samples of 100 queries are fixed by a hash of the query text, not by results.
* Timestamp literals of the form `'YYYY-MM-DD HH:MM:SS'` are read by the SMT prover (they were declined before, which left STATS at 17 of 100). A proof that mixes date-only and timestamp literals is still declined.
