# Trying rewrites across LLM-R2's query sets

[Simple eval index](README.md) · [Full reference](../../docs/evals/llmr2-bench.md)

This is a scale test using many queries collected by LLM-R2. The name describes the source project; KumoSQL's rewrite rules run without a language model.

## What is measured?

The report separates queries changed, changes proved, agreement on real data, unchanged queries, unsupported conversions, crashes, and timing or plan effects.

A flat join may already have nothing for cleanup rules to change. That is a useful preservation check, but it is not a successful improvement.

## Understand the split

Training query files were available during development. Test files were held out and evaluated separately. The suite downloads the pinned query files into a cache rather than checking them into this repository; the source has no license file.

The large set includes JOB-style joins and TPC-H/DSB workloads. Lifting a subquery may change the printed SQL without making execution faster: DuckDB can already simplify the original internally.

A changed query that is proved and matches on real data has correctness evidence. A speed claim additionally needs the recorded engine measurements.

Use the full guide for downloads, real-data prerequisites, train/test numbers, and rerun instructions. [Transformation workloads](transformation-bench.md) explains the related measurements.
