# Feedback-driven SQL optimization run bundle

[Plain-language version](../../docs_simple/evals/feedback-optimization.md)

The feedback-driven SQL optimization repository publishes checksummed archives of selected experiment runs. The pinned [June 30, 2026 real-world TPC-H SF1 run](https://github.com/KostovMartin/mk-feedback-driven-sql-optimization/blob/2d6de251f3befec6ef473b77196510243f59f2d5/experiment-results/real-world-sf1-mixed-duckdb-20260630-160514.md) has original templates, generated candidate SQL, and `full_comparison` equivalence checks. The archive contains 11 candidates: ten passed checks and one candidate whose original query DuckDB could not execute. Only the ten successful checks are scored.

The source marks a candidate safe after comparing the original and rewrite on up to three parameter sets over generated TPC-H SF1 data. These are empirical claims, not universal equivalence labels. `tools/feedback_optimization_bench.py` downloads the GPL-3.0 archive at the pinned commit into the benchmark cache, checks its SHA-256 (`5df49e439afa6299d02488ebf40f73cf480a53089f93cb552b84aa4cf43d261f`), and never stores the SQL in this repository. The harness adapts each bind to a scalar `MAX` over a synthetic typed parameter table, then scores the resulting pair against the TPC-H schema. KumoSQL attempts a proof and searches for a counterexample on schema-valid synthetic data. A proven pair with a replayed counterexample counts as wrong.

On the ten passing checks, KumoSQL proved six pairs, left four unknown, and found no refutations or false proofs. Both held-out pairs were proved. These scores describe the harness and prover on this small selected run; they do not validate the source's performance claims.

The archive omits the TPC-H data, workload manifest, and parameter files. The source's exact checks cannot be replayed from the archive, and its parameter values are unavailable. The harness adapts each bind to a scalar `MAX` over a synthetic typed parameter table. This keeps each bind symbolic for proof and makes every possible bind value available to counterexample search; the source SQL is still the starting point for both sides. The eval splits pairs into development and held-out sets by SHA-1 of the candidate id. Every source SQL pair was inspected during the initial source-gate investigation, so results are marked **tuned on test**.

```sh
python tools/feedback_optimization_bench.py --split dev
python tools/feedback_optimization_bench.py --split held-out
python tools/feedback_optimization_bench.py --write-results
```
