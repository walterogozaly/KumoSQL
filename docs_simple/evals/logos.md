# Checking Calcite rewrites of TPC-H, DSB and TPC-DS queries

[Simple eval index](README.md) · [Full reference](../../docs/evals/logos.md)

The Logos project collects pairs of SQL queries: a benchmark query, and a rewritten version of it. KumoSQL scores 73 of those pairs. For TPC-H and DSB, the rewrite is what the Calcite optimizer prints after applying one rule. For TPC-DS, it is the kit's own alternative formulation of the query.

## A concrete example

TPC-H query 1 averages a column. Calcite's rule `AGGREGATE_REDUCE_FUNCTIONS` rewrites `AVG(x)` into a sum divided by a count, rounded to two decimal places. KumoSQL runs both versions on generated TPC-H data and finds that `avg_qty` is 25.537587 in one and 25.54 in the other. The pair is not equivalent, so the checker must never claim a proof for it.

## How it decides

Each pair goes through two checks that do not trust each other.

1. KumoSQL tries to prove the two queries return the same rows on every database that fits the declared schema.
2. KumoSQL runs both on the generated benchmark data and on many small made-up databases, and looks for a difference. A difference only counts when DuckDB gives the same rows with its optimizer switched off.

A pair is counted as proved only if the proof succeeds and no database separates the queries. A proof that a database contradicts would be a wrong answer, and the eval is built so that count stays at zero. When neither check settles a pair, the answer is unknown, and that is better than a guess.

## Where the cases live

The query text comes from the TPC benchmark kits, so it is downloaded at run time from a pinned version of the Logos repository and checked by hashes. It is not copied into KumoSQL's repository. About one pair in five is held out and never used while developing the checker.

## Limits of the evidence

- A Calcite rule's output is a golden rewrite, not a proven answer. The few pairs a database shows different are reported as problems with the pair, not as KumoSQL failures.
- Most of the unproved pairs use syntax KumoSQL cannot yet print faithfully, such as `ROLLUP` or `NULLS FIRST`. They are counted as unsupported, not as wrong.
- The generated data differs from the official benchmark data, and a few proofs can time out on a busy machine.
- Some of the held-out pairs were seen in a baseline listing, which the full guide records as "tuned on test".

The full guide has the commands, the source pins, the label failures, the overlap with other evals and the recorded scores.
