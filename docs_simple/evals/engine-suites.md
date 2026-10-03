# Checking rewrites on database test suites

[Simple eval index](README.md) · [Full reference](../../docs/evals/engine-suites.md)

Database projects already have thousands of queries with setup data. This evaluation uses DuckDB and SQLite SQLLogicTest corpora and SQLGlot fixtures to compare queries before and after KumoSQL rewrites.

## How a case works

1. Build the case's data in a fresh in-memory DuckDB.
2. Check that translating the original into BigQuery SQL and back preserves its result.
3. Apply the KumoSQL rewrite and execute it.
4. Compare the rewritten result with the control result.

The translation control helps separate a dialect-conversion problem from a rewrite problem. Unsupported setup directives, nonrepeatable queries, and translation failures are counted rather than hidden.

## What this establishes

It can reveal a behavior-changing rewrite on the tested data. It records the prover's verdict too, allowing an execution mismatch to challenge an accepted proof.

These execution comparisons use bags of rows, so duplicate counts matter but row order is not checked. They do not prove equivalence for all possible data.

The SQLite corpus is sampled and run in DuckDB; its row does not mean every SQLite query was checked in native SQLite. The full guide lists pins, licenses, sampling, skips, resume options, and rerun commands. Full runs can take hours; start with the documented sample when working on the harness.
