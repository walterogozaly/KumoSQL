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

## What "0 wrong" means for cost recommendations

The cost-recommendation eval ran KumoSQL's suggested rewrites of 156 TPC-DS and DSB queries against PostgreSQL and compared the results with the originals. There were 72 suggestions: 65 returned the same answer as the original, 7 could not be judged (the original query would not run), and none was seen to give a wrong answer, including under the stricter comparison described below. That is a count on one workload, not a guarantee.

Two separate questions are kept in separate fields. One is whether KumoSQL could prove the rewrite equivalent, which holds only under stated assumptions (declared keys are true, no NaN, errors such as division by zero are not modeled, tied rows may come back in another order). The other is whether the two queries returned the same answer on this one database, which can never show they always will.

An example of what a comparison can miss: `WHERE k = 10` and `WHERE k < 15` return the same row on a table that has no k from 11 to 14. The comparison checks the order of results too (a sort column that is not shown is added to both queries for the check), the output column names and types, and floating-point numbers to 9 decimal places. A suggestion that costs up to 2% more by the estimate can still be accepted, so accepted does not mean cheaper. There is no held-out split, and build recommendations, shared models, cost attribution and change reports are outside this score.

The stricter comparison has been rerun on freshly generated TPC-DS and DSB data (the generated queries are not stored, so the 156 queries are a new draw from the same templates and seed, not the identical text of the first run). The suggestions agreed and none was wrong. The [full reference](../../docs/evals/rewrite-benchmarks.md#cost-recommendation-validity) has the exact rules, the recorded numbers and what changed from the first run.

### The same question for "store this view" and "make this table a view"

The materialization advisor suggests two other kinds of change: store a view as a table (so readers stop recomputing it) or turn a table back into a view. These change when a model's rows are computed, not what any query says, so the check is a different one. Every suggestion carries a label: proven, needs proof (it depends on something the advisor cannot see, such as a source that never changes between refreshes), or refused (the model reads the clock, a random number or a UUID, so a stored copy would freeze a value the view recomputes). Only proven suggestions can count as a saving; the rest are listed as "needs proof" or refused.

An example: a view that adds `CURRENT_DATE()` to every row. Stored as a table at 06:00 it shows that morning's date until the next refresh, while the view shows today's date at every read. The eval builds invented projects in DuckDB, runs each with and without the change for three simulated days, and compares what every query and every model returns before and after each refresh. On 19 suggestions across three invented projects, the 7 proven ones and the 3 chosen sets returned identical rows, and the 5 refused ones did return different rows. Six of the 7 "needs proof" suggestions returned stale rows between refreshes; the seventh agreed because its condition holds in the simulation, and it is still not counted. No unproven suggestion was counted as a saving.

The limits: the projects, job histories and sizes are invented and small, written alongside the advisor, so this shows that the labels and the gate behave on these shapes, not how often the advisor is right on a real warehouse. The savings are estimates, not measurements. The [full reference](../../docs/evals/rewrite-benchmarks.md#advisor-recommendations-store_view-and-unstore_table) has the rules, the table of counts and the simulation's assumptions.

The full guide contains setup commands, data sizes, proof conditions, score definitions, and measured results. Start there before rerunning: these suites can require database servers and large datasets. [Cost reports](../cost-and-change-reports.md) explains how the product presents the same distinctions.
