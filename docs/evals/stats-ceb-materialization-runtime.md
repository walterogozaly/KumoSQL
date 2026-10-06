# STATS-CEB materialization runtime model

[Plain-language version](../../docs_simple/evals/stats-ceb-materialization-runtime.md)

This diagnostic evaluates whether a runtime model can rank proven query rewrites that read a stored join view. It measures one `COUNT(*)` query at a time against a stored view, extracts KumoSQL and DuckDB plan features, fits only on development query-view pairs, and scores predictions on the fixed held-out query split. It does not yet evaluate the full advisor or claim that materializing a view reduces total workload cost.

## Source and command

The input is the 146-query STATS-CEB workload and simplified database from [End-to-End-CardEst-Benchmark](https://github.com/Nathaniel-Han/End-to-End-CardEst-Benchmark), pinned at commit `670cb8d4bf4cbfa32f94fdf17f33973d3fd67d1b`. The source repo and database are downloaded or built outside KumoSQL's checkout.

```powershell
git clone https://github.com/Nathaniel-Han/End-to-End-CardEst-Benchmark
git -C End-to-End-CardEst-Benchmark checkout 670cb8d4bf4cbfa32f94fdf17f33973d3fd67d1b
python tools/materialize_stats_bench.py --repo End-to-End-CardEst-Benchmark
```

The command uses the full workload by default. Query/view proofs, per-query baselines, per-view measurements and the final score are resumable under `KUMOSQL_BENCH_DATA` (default `~/.kumosql-bench/materialize-stats`). The stable 1-in-4 query-name hash split is fixed by the pinned source order. Candidate views are mined from development queries only; the held-out features and measurements are released after that set is frozen. DuckDB uses one thread and a 1 GB memory limit by default; the timeout and repetition count can be changed on the command line and are recorded in the cache manifest.

For a short plumbing check, the same full pipeline can run on a deterministic 24-query sample:

```powershell
python tools/materialize_stats_bench.py --repo End-to-End-CardEst-Benchmark --sample 24 --timeout 10 --reps 1 --proof-timeout-ms 3000 --resamples 100 --threads 1 --memory-limit 1GB
```

That sample is diagnostic only and is not written to the benchmark scoreboard.

## Diagnostic sample

On 2026-10-06, the 24-query sample selected 17 development and 7 held-out queries. It froze 56 development-mined views, measured 169 development and 20 held-out proven query-view pairs, and found 0 wrong held-out results. The best-looking predeclared model in this tiny sample was `levels_anchored`: Spearman 0.64 (group-clustered 95% interval 0.48–0.78). Only seven held-out queries and five held-out view groups contribute, so this is not a stable score or a model-selection result.

This diagnostic used one timing repetition, a 10-second baseline-query timeout and a 1 GB memory limit. The first uncapped full-run attempt was stopped when one query grew to about 2.2 GB and left less than 1 GB free on the 12 GB machine. The bounded runner now checkpoints each baseline and view, but the full 146-query score has not been measured. No result row or floor is claimed.

## Remaining evaluation

The full run must produce a stable held-out score and a checked-in results row before this becomes a benchmark floor. The advisor-versus-everything-a-view comparison is also outstanding; a good pairwise runtime ranking alone does not show that the chosen view set lowers total workload runtime after refresh costs.
