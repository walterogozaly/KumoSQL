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

The table checks include statements that change tables. A rename keeps the old input name and new output name,
and a script writing two tables checks both outputs. A partition name uses the base table's schema when that schema
is known. The full reference records the current scores and parser version.

Matching these table connections does not prove complete column tracing. Nested fields of a stored STRUCT still
trace only to the containing column, and unsupported column shapes remain unknown.

The full guide lists source versions, exclusions, disputes, and development exposure. Use it to see whether a result came from a genuinely independent test and which mismatches were inspected during development.
