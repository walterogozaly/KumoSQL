# Testing the impact of column changes

[Simple eval index](README.md) · [Full reference](../../docs/evals/schema-change-bench.md)

This evaluation asks whether KumoSQL identifies the models affected by a schema change: removing, renaming, or changing a column.

The generator builds connected model projects and knows which columns each model needs. A separate simulator supplies the expected effect without parsing the generated SQL.

## Example

If `customer_totals` reads `daily_sales.total`, dropping `daily_sales.total` breaks that direct reader. Models reading `customer_totals` can be affected indirectly.

A query that never reads the changed column should not be reported as a definite direct break. Unsupported tracing should be marked unknown rather than silently treated as unaffected.

Column types are part of the answer: if `daily_sales.total` goes from `INT64` to `FLOAT64`, a model that stacks `daily_sales` on top of another table with `UNION ALL` now outputs `FLOAT64` for that column, and the report should say so. KumoSQL reads output types with sqlglot and checks them with its own GoogleSQL type checker, which is only used when it is sure; when it is not sure, the older answer stays. The generated projects include this union case, and the full reference records how the score moved. The evidence covers numeric types only; other type combinations are covered by unit tests, not by this evaluation.

## Run it

From a development checkout:

```sh
python tools/schema_change_bench.py
```

The suite uses several pipeline sizes and seeds to check both correctness and scale. The full reference lists the change families, outcomes, and results file.

These are generated projects with an independent answer key, not a guarantee that every real application's SQL and external consumer has been discovered. For your own project, inspect the completeness diagnostics alongside the impact report. See [pipeline analysis](../pipeline-analysis.md).
