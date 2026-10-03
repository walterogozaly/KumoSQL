# Using Spider 2.0 queries to test analysis

[Simple eval index](README.md) · [Full reference](../../docs/evals/spider2-bench.md)

Spider 2.0 is normally a text-to-SQL benchmark. KumoSQL repurposes reference BigQuery queries as inputs to its SQL analyses. It does not score generating SQL from a question.

## What gets tested?

Queries run through applicable stages such as parsing, dependency tracing, cleanup, formatting, and checking. The suite records passed, failed, unsupported, timed-out, and error cases per stage.

Original reference queries and adapted cases are kept separate. An adaptation can make a feature testable without establishing that the original query was fully supported.

## Read the denominator

The supported-subset percentage excludes unsupported cases. Read it together with the total coverage and unsupported count; a good percentage on a small supported subset is not full corpus support.

The harness uses CPU-time limits and a wall-clock backstop, so a slow query or blocked process does not hang the suite indefinitely.

The corpus is recorded with file hashes. Its collection notes explain that the upstream commit could not be read at collection time; do not treat that as a verified commit pin.

The full guide lists source provenance, exclusions, adaptations, fixes found by the suite, and recorded results. See [analytical coverage](analytical-sql-coverage.md) for a similar multi-stage evaluation.
