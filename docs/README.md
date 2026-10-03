# KumoSQL documentation

Start with [Getting started](getting-started.md): install, a first verified rewrite, a pipeline impact report and the browser UI. The [README](../README.md) has the benchmark scoreboard, a summary of every feature and the CLI table.

## Using KumoSQL

| Page | What it covers |
| --- | --- |
| [Local browser UI](ui.md) | Starting the UI, its pages, saved state, scopes, data sources and tags |
| [Connecting Dataform repositories](dataform-repositories.md) | Private repositories through local `git`, the data folder, logging and diagnostics, production schedules |
| [Rewrite rules](rewrite-rules.md) | The rule registry and the subquery lifter |
| [Equivalence provers](provers.md) | Structural prover, synthetic-data comparison, Z3, the algebraic prover and SQLSolver |
| [Whole-pipeline analysis](pipeline-analysis.md) | Loading a project, lineage and impact, table profiles, work already done elsewhere, comparing outputs |
| [Cost, change reports and the BigQuery dry run](cost-and-change-reports.md) | Dry-run checks, cost attribution, change reports and refactoring proposals |
| [Refactor](refactor.md) | Protected and editable tables, searching for simpler pipelines, and folding chosen tables into one |
| [Table minimization](table-minimization.md) | The lowest-complexity set of tables that keeps every protected table proved unchanged |
| [Project reduction](project-reduction.md) | The smallest Dataform project that still produces the outputs you keep, as a proved patch on the `.sqlx` files |
| [Output properties](output-properties.md) | Never-NULL columns, unique keys and row bounds, inferred without running a query |
| [Constraint-dependent rewrites](constraint-rewrites.md) | Rewrites that hold only under declared NOT NULL columns, keys and foreign keys |
| [Incremental models](incremental.md) | Whether an incremental run equals a full refresh |
| [Join ordering and cardinality](joinorder.md) | Sub-join size estimates and join orders, in pure Python |
| [UI roadmap](ui-roadmap.md) | The JSON each graph, cost and change view reads |
| [BigQuery test bed](bigquery-testbed.md) | A messy, low-cost model layer with real job history |
| [Multi-statement scripts and MERGE](scripts.md) | Splitting BigQuery scripts, following temporary tables and variables, MERGE lineage, job history, and the script eval |
| [Model reuse and containment](model-reuse.md) | View reuse, query containment, aggregate decomposition |
| [Public SQL evaluation sources](public-sql-evaluation-sources.md) | The research list of public suites, databases and projects; what KumoSQL already scores from it is in the [inventory](evals/public-sources.md) |
| [Test history](test-history.md) | Recording every test run and its times, ranking the tests that break changes that otherwise work, tracing test times over time, and running likely failures first |

## Evals

The [evals folder](evals/README.md) has one page per eval family (the SQLSolver, VeriEQL, Singh and Bedathur, SQL-IQ and LLM-SQL-Solver equivalence suites, DLBench's cross-dialect translations, pairs from optimizer wrong-result bugs, bounded verification, rewriting benchmarks, engine test suites, syntax and behaviour coverage, lineage and Dataform evals, fuzzing) and a table naming the `benchmarks/results/*.json` file behind every eval. Each eval's numbers are in the README scoreboard ([format](../benchmarks/README.md)).
