# Comparing lineage with other projects

[Simple eval index](README.md) · [Full reference](../../docs/evals/lineage-goldens-bench.md)

A “golden” is a test's stored expected result. This evaluation compares KumoSQL's table and column dependencies with expected lineage from DataHub and OpenLineage.

## Why keep the suites separate?

OpenLineage uses a different parser, so it provides an independent comparison. DataHub shares SQLGlot-based machinery with KumoSQL, so agreement can share the same underlying mistake. The results are not added into one independence claim.

## What the labels mean

- **Exact** matches all expected connections.
- **Coarse** traces a nested field only to its containing column, with no extra connection.
- **Unknown** admits incomplete tracing.
- **Missed** or **wrong** is a confident missing or extra dependency.
- **Disputed** records a reviewed difference in what the two tools mean by lineage.

The harness must adapt naming conventions without changing the expected meaning. For example, shard names and nested fields need careful comparison.

BigQuery-relevant cases are distinct from other-dialect cases read as BigQuery. The latter are a generalization check, not part of the same headline score.

The full guide lists source versions, exclusions, disputes, and development exposure. Use it to see whether a result came from a genuinely independent test and which mismatches were inspected during development.
