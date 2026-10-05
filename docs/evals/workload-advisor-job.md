# JOB workload-advisor proxy

This eval measures whether the workload advisor can choose proven join views that lower total query runtime on the Join Order Benchmark (JOB). The source is `danolivo/jo-bench`; its repository is BSD-2-Clause, while the IMDb data's terms are not verified here. The benchmark keeps the data and caches outside the KumoSQL checkout.

## Split and protocol

Families 1–16 (59 queries) are the development set. Candidate joins, proof-approved readers, the bounded view sample, feature fitting and advisor selection use only those queries. Families 17–33 (54 queries) are held out. The harness does not parse, prove or execute held-out SQL until the selected views are fixed; that split measures only the frozen selection and never feeds it back into candidate choice or fitting.

The harness mines joins shared by at least two development queries, with at most four tables. It scans candidates in their deterministic support ranking, keeps views up to a 30-million-row cap, and measures at most ten. Each reader must pass `rewrite_over_model` and an executed result-bag comparison. A DuckDB disagreement counts as wrong only when the same difference remains with DuckDB's optimizer disabled. Views are built once per workload day; `--query-runs-per-day` sets how often each query runs, and this local proxy does not price storage. JOB publishes SQL templates without schedules, so that frequency is an explicit scenario assumption rather than an observed production schedule.

KumoSQL's cardinality features (`scan`, `join`, `write`, `query`, `rows`) are fit to measured development baselines, proven reads and view builds. The advisor selects with those predicted costs. The holdout report includes selected view build time and routes each query using the frozen model's predicted cheapest option; it compares total measured runtime with the original-query baseline.

## Run

Clone the source and keep DuckDB's database and caches outside the repository:

```powershell
git clone --depth 1 https://github.com/danolivo/jo-bench JOB
$env:KUMOSQL_BENCH_DATA = "$env:TEMP\kumosql-job-cache"
python tools/workload_job_bench.py --repo JOB --query-runs-per-day 10 --write-results
```

`--max-views`, `--max-candidates`, `--row-cap`, `--sample-rows`, `--query-runs-per-day`, `--proof-timeout-ms`, `--execution-timeout` and `--repeats` bound or configure a run. The checked-in scenario assumes ten runs per query per day so one daily view build can be amortized; use `--query-runs-per-day 1` to test a single daily pass. The command records the source commit, split, cost-model error, selected views, timings, proof counts and result mismatches in `benchmarks/results/workload-advisor-job.json` and regenerates the README scoreboard.

## Measured result

On the checked-in 10-view run, the model selected one 2.61-million-row view. At the stated ten-runs-per-query/day assumption, development runtime improved from 99.16 s/day to 94.54 s/day including the build. The held-out workload regressed: 160.49 s/day with the advisor versus 154.14 s/day for the baseline (4.1% slower, including a 0.77 s view build). Thirty-seven held-out rewrites were proved and executed, with 0 wrong and no optimizer-only disagreements.

The calibrated runtime model is not reliable enough yet: development reader-savings Spearman is 0.085 (95% pair-bootstrap interval −0.054 to 0.220), and top-10 precision is 0.30 (0.00 to 0.70). The bootstrap resamples pairs without clustering repeated queries, so these intervals may be too narrow. Its in-sample cost q-error is p50 2.56 / p90 6.43; the held-out chosen-route cost q-error is p50 3.87 / p90 7.52. Twenty-one held-out baseline queries and twelve rewritten reads needed the scan-only fallback because their join keys were absent from development statistics. This experiment therefore does **not** meet the goal that the advisor beat the baseline on held-out JOB runtime; it packages the reproducible failure evidence for the next model iteration.

These are single-machine DuckDB timings, scaled from measured per-query runtimes to the explicit ten-runs/day scenario. They are not evidence of warehouse cost or of a globally optimal view set. The development result is reported separately and is not a holdout score.
