# Testing reuse of shared joins

[Simple eval index](README.md) · [Full reference](../../docs/evals/mv-benchmark.md)

A materialized view stores the result of a query so other queries can reuse it. This benchmark asks whether shared joins can supply other workload queries with a verified rewrite.

## What happens?

KumoSQL looks for joins shared by development queries and proposes views exposing the needed columns. It selects a small set of candidates, then tries rewriting both development and held-out queries over them.

Each accepted replacement is proved with the view's definition expanded. Proofs are also checked on random databases.

## What does held-out mean here?

JOB is split by query family. Other workloads use a query-text hash, so a held-out query may be another instance of a template seen in development. Candidate views are mined only from development queries.

Read that split carefully: success on a familiar template is different from success on entirely new shapes.

## What the score does not measure

The benchmark does not build the views in an engine or time their use. A valid reuse rewrite does not itself establish saved runtime, storage cost, or refresh cost.

The random databases are small and can leave some large joins empty, which limits that cross-check. The full guide lists workload sources, overlap with other evals, pinned downloads, licensing notes, and results.

See [model reuse](../model-reuse.md) for the everyday distinction between reuse, containment, and summary rebuilding.
