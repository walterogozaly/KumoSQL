# Testing BigQuery-specific behavior

[Simple eval index](README.md) · [Full reference](../../docs/evals/bigquery-behavior-eval.md)

This suite checks whether accepted rewrites preserve tested behavior on GoogleSQL compliance queries and BigQuery edge cases.

For example, lifting a subquery must not lose a PIVOT or UNPIVOT attached to it. A changed column type can also be a real behavior change even when some values look the same.

## How it checks

KumoSQL rewrites the query. For supported cases, the original and rewritten SQL are translated to DuckDB and executed. Results are compared with duplicate counts, or as ordered lists when ORDER BY applies.

The report distinguishes handled rewrites, declined changes, unsupported parsing, crashes, and originals DuckDB cannot execute. **Wrong** means an accepted rewrite changed the observed result.

## Try the edge cases

From a checkout with development dependencies:

```sh
python tools/bq_behavior_eval.py --corpus edge --failures --details
```

The full guide provides the larger GoogleSQL run and recorded scores.

This is local execution of translated SQL, not a run of the full suite on BigQuery itself. Translation and engine differences limit what it establishes. Read coverage alongside “0 wrong”: unchanged or declined queries do not count as useful rewrites.
