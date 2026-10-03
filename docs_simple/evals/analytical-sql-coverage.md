# Coverage on analytical SQL

[Simple eval index](README.md) · [Full reference](../../docs/evals/analytical-sql-coverage.md)

This suite asks whether KumoSQL can handle large analytical queries from TPC-DS, DSB, and SQLStorm. These have nested queries, grouping, joins, and window functions beyond small syntax examples.

## What counts as success?

A query goes through parsing, project loading, graph and column tracing, fingerprints, cleanup, formatting, proof, and execution checks.

- **Clean** means no crash, timeout, or silent damage. Explicitly reporting unsupported SQL can still be clean.
- **Full** means every applicable stage supplies a real answer.

Queries that cannot be converted into BigQuery SQL are reported separately, outside the score. Read the conversion count as well as the supported-subset percentage.

Rewritten queries are executed in DuckDB on generated data. A rewrite labeled proven but returning different rows is a failure. A difference that the verifier refused to accept is recorded as caught.

## What the score does not establish

Generated-data agreement is a check on the accepted proofs, not a replacement for a proof. It does not establish production speed or support for every BigQuery feature. Some tie-sensitive windows, unordered aggregates, and LIMIT cases remain unknown for good reason.

The full guide lists the corpora, stages, gaps, and recorded results. For real-data timing, see [transformation workloads](transformation-bench.md).
