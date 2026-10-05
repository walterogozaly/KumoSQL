# A Python interpreter for BigQuery SQL

[All simple guides](README.md) · [Full reference](../docs/gsql-eval.md)

KumoSQL often needs to know what a query returns. Today it asks DuckDB, after translating the SQL, and DuckDB sometimes answers differently from BigQuery. The GoogleSQL evaluator is a second opinion written from BigQuery's own rules: a small database engine in plain Python that runs a query on tables you hand it and returns the rows BigQuery would return. If it is not sure how BigQuery behaves, it says so instead of guessing.

For example, give it a table with a number column holding `1` and `NULL`, and ask for `SELECT a + 1 FROM t ORDER BY 1`. It returns `NULL` first and then `2`, the way BigQuery orders NULLs. Ask it for `1 / 0` and it reports a runtime error, the way BigQuery does, while `SAFE_DIVIDE(1, 0)` returns `NULL`. Ask for a function it has not been taught and it answers "unsupported". It can also tell you that a result depends on something BigQuery leaves undecided, such as which row `ANY_VALUE` picks.

## Why it exists

Its job is to be an independent check. When the DuckDB path and this evaluator give the same rows, that is more convincing than either alone. When they differ, one of them is wrong, and the difference is worth a look. It is not yet used by the checks that find counterexamples; it is the foundation for that.

## Limits of the evidence

- It is checked against Google's published compliance tests, not against live BigQuery. Where those tests do not settle a behaviour, the evaluator declines.
- It does not cover everything: columns of types BigQuery lacks, JSON functions, ranges and a number of aggregate corner cases are declined.
- On the files it was built against it matches 85.6% of the claimed cases and gets none wrong. On a set of files kept aside, its first run matched 61.9% with 30 wrong answers, which were then fixed. Treat 62% as the realistic figure for queries it has not been tuned on.

The full reference lists what it covers and how it is organised, and the [conformance eval](evals/googlesql-conformance.md) has the recorded scores. See the [full reference](../docs/gsql-eval.md) for the API.
