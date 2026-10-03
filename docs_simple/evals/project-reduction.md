# Testing how far a Dataform project can shrink

[Simple eval index](README.md) · [Full reference](../../docs/evals/project-reduction.md)

This suite gives [project reduction](../project-reduction.md) a whole Dataform project and a list of outputs to keep, then checks the patch it returns. A kept output must keep its name and give exactly the same rows and columns. Everything else may be deleted or rewritten.

## The projects

- **Generated projects.** Each table-minimization case is written as a Dataform project, with assertions, a project variable, incremental tables and `dependencies` mixed in so the reducer meets them together.
- **Open-source projects.** Eight public Dataform projects, with each final table kept alone and all of them kept together.
- **Jaffle Shop.** dbt Labs' small example shop, written as a Dataform project, with its real seed data.

Some cases are held out: they were not looked at while the reducer was built and were run once at the end.

## How it is checked

Each patch must apply with `git apply`. Generated projects and Jaffle Shop are then run on DuckDB, before and after, on many small databases, including ones built to catch common mistakes; any difference in a kept output or a surviving assertion counts as wrong. The open-source projects have no data, so they count only when KumoSQL re-proves every kept output of the patched project.

The score reports how many projects got smaller, how many were wrong, and how much of the project's complexity was removed, next to what simply deleting unneeded tables would remove. The full guide has the numbers, the one wrong answer found during development and how it was fixed, and the limits of the evidence.
