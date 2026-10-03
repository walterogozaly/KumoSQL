# Checking a refactor across several models

[Simple eval index](README.md) · [Full reference](../../docs/evals/pipeline-equivalence.md)

Single-query equivalence checks two statements. Whole-pipeline equivalence checks the outputs people can still read after several connected models change.

For example, a filter might move from a final report into an upstream model. Checking only the final report's new SQL without the upstream change would miss part of the question.

## What this suite contains

The cases were written for this evaluation. They include equivalent refactors and changes designed to break observable results, with several model boundaries between inputs and outputs.

The report counts proved, refuted, unknown, unsupported, timeout, and error outcomes. It also counts changed pipelines and changed outputs: an output whose model and inputs did not change is trivially equal and should not inflate proof coverage.

## Read proof and refutation separately

A proof should preserve every required output under its assumptions. A refutation should include a database demonstrating a changed result. Proving a deliberately different case or refuting an equivalent one is wrong.

Rejecting a refactor is not a successful simplification. Unknown is an honest limit, and it remains visible.

The full guide explains the model families, held-out cases, overlap with single-query corpora, and recorded results. See [Refactor](../refactor.md) for choosing protected outputs in your own project.
