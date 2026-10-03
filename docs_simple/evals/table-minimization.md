# Testing whether a pipeline can use fewer tables

[Simple eval index](README.md) · [Full reference](../../docs/evals/table-minimization.md)

This suite tests pipeline simplification methods, including the Refactor search and the dedicated table minimizer: can they remove or combine intermediate models while keeping protected outputs identical?

For example, an unused staging model can be removed. A staging query used by one report might be moved inside that report. Both changes need to preserve any outputs that remain observable.

## What is measured?

The cases have known simplification opportunities and traps. The evaluation checks the returned candidate against the original and compares its reduction with a known reference reduction.

A protected output disappearing or changing is wrong. A case left unchanged may be safe, but it is not a successful simplification. The scores therefore report correctness and amount of reduction separately.

## Why the search may stop early

The prover may not support a move, or the search may reach its time or state limit. Fewer tables can also increase SQL complexity when their queries are inlined. There can be several useful tradeoffs rather than one best pipeline.

The reference pipeline is a comparison target, not a guarantee that the search finds a global minimum. Unknown or rejected candidates stay visible.

The full guide contains case formats, development and held-out families, verification methods, and results. [Refactor](../refactor.md) explains protected, editable, and unchanged model classes; [table minimization](../table-minimization.md) shows an explicit set of queries as input.
