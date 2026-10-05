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

## The BigQuery Utils UDF tests

A second part asks a different question: does KumoSQL's BigQuery-to-DuckDB translation give the same value BigQuery gives? Google's open-source `bigquery-utils` project ships unit tests for its SQL functions (inputs and the output Google checked on BigQuery). Each test becomes a query that calls the function body on those inputs. KumoSQL translates it to DuckDB, runs it, and compares the value with the expected output.

For example, `DIV(7 + 1, 2 * 2)` must give 2; plain translation printed it so that it computed `7 + (1 // 2) * 2` instead. The run found about sixty such translation bugs (array positions, `FORMAT`, day-of-week numbering, regular-expression misses and more), and each fix is a general one, kept as a regression test. A case counts as wrong only if it returns a different value; cases KumoSQL cannot translate or DuckDB cannot run are counted as unsupported, never as agreeing.

```sh
python tools/bq_utils_udf_eval.py
```

Limits: more than half of the cases are JavaScript or Python functions and have no SQL to translate; a fifth of the cases are held out and never printed while fixing; and DuckDB is not BigQuery, so a passing value shows the translation models that function well, not that every query is safe. The full guide has the recorded scores.

This is local execution of translated SQL, not a run of the full suite on BigQuery itself. Translation and engine differences limit what it establishes. Read coverage alongside “0 wrong”: unchanged or declined queries do not count as useful rewrites.
