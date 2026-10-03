# Trying transformations on real benchmark data

[Simple eval index](README.md) · [Full reference](../../docs/evals/transformation-bench.md)

This evaluation applies KumoSQL transformations to TPC-H, TPC-DS, and the Join Order Benchmark (JOB), using real generated or benchmark data.

## What it separates

A query may parse, change, receive a proof, match the original on the loaded data, and run faster. Those are separate observations, and the report keeps them separate.

Unchanged SQL is not counted as a useful rewrite. A verified rewrite that produces the same engine plan is correct but may provide no speed improvement.

JOB also includes alternative query forms to test whether supported transformations recover equivalent shapes beyond the original wording.

## What to expect before running

These runs need the workload query files, schemas, data, and local engine setup. They are more involved than a quick unit test. Use the full guide's pinned sources and commands to reproduce the measurement.

TPC-style data is generated at a specified scale; JOB uses its own dataset. Runtime is specific to that scale, engine, and machine.

Agreement on the loaded data cross-checks a proof but does not establish equivalence by itself. Timeouts, unsupported conversions, and gaps remain part of the result.

The full reference lists workloads, alternative forms, measured results, and limitations. See [rewrite benchmarks](rewrite-benchmarks.md) for other performance suites and [analytical coverage](analytical-sql-coverage.md) for multi-stage support on generated test data.
