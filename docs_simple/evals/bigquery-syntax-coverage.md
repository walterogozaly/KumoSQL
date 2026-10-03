# Testing BigQuery and Dataform syntax

[Simple eval index](README.md) · [Full reference](../../docs/evals/bigquery-syntax-coverage.md)

This suite has small examples of BigQuery and Dataform constructs: queries, scripts, table changes, functions, SQLX configuration, and project layouts. It checks each applicable stage of KumoSQL.

## Three outcomes

| Outcome | Meaning |
| --- | --- |
| Pass | The stage handled the construct correctly |
| Unsupported | The stage explicitly declined it without damaging it |
| Fail | A crash, lost dependency, or changed meaning occurred |

Unsupported cases are recorded in `tests/fixtures/bq_syntax/known_gaps.json`. A newly unsupported case needs an explanation rather than silently disappearing.

## Why this matters

A tool can parse some SQL yet misunderstand a reference inside Dataform JavaScript, change a quoted function name, or lose a dependency in an operation block. Testing several stages catches problems that a parsing-only score misses.

Some procedural and newer BigQuery forms are kept as opaque text. Preserving them with a clear diagnostic is different from fully analyzing them.

The fixture folder holds a manifest, examples, dry-run records, and known gaps. A dry-run failure can also mean an example names an object absent from the test project. The full guide explains which gaps belong to KumoSQL, its parser, or the external environment.

Syntax coverage does not establish equivalence on every dataset. See [BigQuery behavior](bigquery-behavior-eval.md) for execution checks.
