# Measuring whether rewrites help

[Simple eval index](README.md) · [Full reference](../../docs/evals/rewrite-benchmarks.md)

Equivalent SQL is not necessarily faster SQL. These benchmarks ask both whether proposed changes preserve results and whether they help under the measured workload.

## The main suites

| Suite | What it contributes |
| --- | --- |
| SQL-RewriteBench | PostgreSQL cases with result checks and measured speed |
| WeTune GitHub issues | Query shapes from reported performance problems |
| ClickBench | Timed queries over a wide table |
| Cost-recommendation validity | Whether proposed savings are supported by correctness and cost checks |

KumoSQL's optimizer applies deterministic SQL identities. The suites distinguish a new candidate, an accepted verified candidate, unchanged SQL, and an actual measured improvement.

## Read timings in context

Timing requires real data, an engine, and a repeatable protocol. For example, ClickBench uses warm-up and alternating runs and compares medians. Those numbers describe the tested PostgreSQL environment, not a universal BigQuery speedup.

A rewrite can return correct rows while leaving the engine plan unchanged. A lower estimate is also different from measured savings.

Some queries or dialect features cannot be executed or verified by the harness. Those outcomes and timeouts remain separate from successful rewrites.

The full guide contains setup commands, data sizes, proof conditions, score definitions, and measured results. Start there before rerunning: these suites can require database servers and large datasets. [Cost reports](../cost-and-change-reports.md) explains how the product presents the same distinctions.
