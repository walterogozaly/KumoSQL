# Testing dependencies and change impact

[Simple eval index](README.md) · [Full reference](../../docs/evals/lineage-bench.md)

Lineage says where data comes from. Change impact says which models are affected when that data changes. These are the foundation of the graph and reports.

This guide covers two suites: expected lineage from SQLLineage's public tests, and generated pipelines with known dependencies and change effects.

## Example

If `report.total` comes from `daily.total`, removing `daily.total` should affect `report`. A renamed alias should not hide that dependency.

Generated pipelines know the intended answer from their construction. The answer key is not produced by running the same SQL parser under test.

## Read the outcomes

An exact result has the expected dependencies without extras. A missed dependency is absent from a confident answer. A wrong dependency claims a connection that should not exist. Unknown explicitly reports that tracing was incomplete.

Scores keep correctness, precision and recall, coverage, and performance separate. An unknown column should not be confidently called unused.

Some BigQuery unions align columns by name rather than position. The suite checks that names, missing columns, and inserted NULLs do not mislead column tracing.

The full guide includes SQLLineage attribution, case conversion, tool commands, and measured results. See [pipeline analysis](../pipeline-analysis.md) for using these reports on your project.
