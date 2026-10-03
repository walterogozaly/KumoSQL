# Which public sources are actually integrated?

[Simple eval index](README.md) · [Full inventory](../../docs/evals/public-sources.md)

This inventory connects the research source lists with implementation work. It records whether each source is already scored, being added, or not added with a reason.

## Read the status

- **Covered** identifies an existing evaluation, usually with a results-file name.
- **New** means work is being added; it does not by itself establish that an evaluation has completed.
- **Not added** explains a blocker such as licensing, missing artifacts, download restrictions, or overlap.

The inventory covers evaluation suites, complete databases, before/after SQL, BigQuery projects, and additional research leads. Some sources overlap existing cases, so they cannot simply be added into an independent-test total.

## Before importing a source

Check its pinned version, license, available data, original dialect, and correctness evidence. Code licensing does not automatically cover the database contents. Some material is fetched at runtime instead of redistributed.

Download availability describes the environment used for the recorded inventory check. Your current environment may differ.

The full inventory gives source-by-source details and planned batches. [The first research guide](../public-sql-evaluation-sources.md) and [additional sources](../additional-public-sql-sources.md) explain the original proposals. Recorded benchmark results remain in the matching evaluation guides and results JSON files.
