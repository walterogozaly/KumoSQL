# JOB workload advisor

[Simple eval index](README.md) · [Full reference](../../docs/evals/workload-advisor-job.md)

This test checks whether KumoSQL can choose shared joins to store so later JOB queries run faster.

## Development and held-out queries

Queries from JOB families 1–16 are used to find views, prove reader rewrites, fit a runtime estimate and choose views. SQL from families 17–33 is not parsed, proved or executed until that choice is fixed. The final score compares original query time with the chosen views' query time plus the time to build those views.

Each rewrite must pass the algebraic prover and return the same rows on the local IMDb database. If optimized DuckDB results disagree, the harness checks again with the optimizer disabled. A difference counts as wrong only when that second check also differs.

## What the result means

The local run is bounded to the highest-ranked candidates that fit a 30-million-row limit. JOB has no schedule, so the checked-in scenario assumes each query runs ten times a day and each view builds once. The advisor was 4.1% slower than the baseline on the held-out families, despite improving the development workload. No rewrite returned a different result. This points to a runtime model that needs more work; the full guide has the measurements and limits.
