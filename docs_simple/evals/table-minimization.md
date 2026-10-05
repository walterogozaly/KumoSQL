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

One more limit: the check only looks at the tables you protect. If you remove a table that something outside the pipeline still reads, the score counts that as a reduction, so add such tables to the protected list.

## Cases taken from real projects

Besides the cases written for this suite, there are 128 cases adapted from public projects: sqlglot's own optimizer test queries (split into a few tables), seven Fivetran dbt packages (18 to 49 tables each) and dbt's Jaffle Shop. They are scored separately, so they never change the numbers for the written cases.

For example, a Fivetran package builds dozens of models, and only the final report models are protected. Removing a model that no protected table reads is safe; rewriting the SQL inside a protected model is much harder.

Each case has a reference answer: sqlglot's own simplified query, or a simpler pipeline written by hand and checked on many generated databases, or (for most Fivetran packages) just "remove the tables nobody reads". The hand-written and checked references are better targets than the mechanical ones, so low scores against a mechanical target say little about the optimum.

The limits: the real pipelines are big, so each case runs in its own process with a memory cap and a time limit, and a few Fivetran pipelines hit the limit and count as not improved. One Fivetran reference has since been hand-simplified; the published benchmark run predates that update. The held-out cases were run once. See the [full reference](../../docs/evals/table-minimization.md#sourced-cases) for the recorded scores.
