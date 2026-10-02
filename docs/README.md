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
| [Refactor](refactor.md) | Protected and editable tables, and searching for simpler pipelines |
| [Output properties](output-properties.md) | Never-NULL columns, unique keys and row bounds, inferred without running a query |
| [Constraint-dependent rewrites](constraint-rewrites.md) | Rewrites that hold only under declared NOT NULL columns, keys and foreign keys |
| [Incremental models](incremental.md) | Whether an incremental run equals a full refresh |
| [Join ordering and cardinality](joinorder.md) | Sub-join size estimates and join orders, in pure Python |
| [UI roadmap](ui-roadmap.md) | The JSON each graph, cost and change view reads |
| [BigQuery test bed](bigquery-testbed.md) | A messy, low-cost model layer with real job history |

## Evals

Each eval's numbers are in the README scoreboard, generated from `benchmarks/results/*.json` ([format](../benchmarks/README.md)).

| Page | Evals |
| --- | --- |
| [SQLSolver and the algebraic prover](sqlsolver.md) | SQLSolver Calcite, Spark, TPC-H and TPC-C; R-Bot; QED; Cosette and SPES |
| [VeriEQL](verieql.md) | VeriEQL LeetCode, Literature and Calcite suites |
| [Singh and Bedathur](singh-bedathur.md) | 2,800 LeetCode equivalence pairs |
| [SQLFluff rule fixtures](sqlfluff-fixtures.md) | 850 lint fail-to-fix pairs: semantic fixes proved, layout fixes checked, KumoSQL's formatter against them |
| [SQL-IQ](sql-iq.md) | Equivalence judge, SQL judge and error classification |
| [Query rewriting benchmarks](rewrite-benchmarks.md) | SQL-RewriteBench, WeTune, ClickBench and cost-based rewrites |
| [Transformations on TPC-H, TPC-DS and JOB](transformation-bench.md) | Transformations on standard workloads with real data |
| [LLM-R2 query sets](llmr2-bench.md) | Scale test of the rewrites on LLM-R2's 11,353 queries, test files held out |
| [Analytical SQL coverage](analytical-sql-coverage.md) | TPC-DS, DSB and SQLStorm through every stage |
| [BigQuery and Dataform syntax coverage](bigquery-syntax-coverage.md) | One case per GoogleSQL or Dataform construct |
| [Multi-statement scripts and MERGE](scripts.md) | Splitting BigQuery scripts, following temporary tables and variables, MERGE lineage, job history, and the script eval |
| [BigQuery behaviour](bigquery-behavior-eval.md) | GoogleSQL compliance queries and edge cases |
| [Metamorphic fuzzing](fuzzing.md) | TLP/NoREC, unsafe-rewrite detection, rewrite composition |
| [Targeted test data](targeted-test-data.md) | Targeted databases, multi-database checking, counterexample minimization |
| [Model reuse and containment](model-reuse.md) | View reuse, query containment, aggregate decomposition |
| [MV-based rewriting benchmark](mv-benchmark.md) | View mining and rewriting on JOB, SCALE, STATS and TPC-DS |
| [Duplicate detection](duplicate-detection.md) | Exact and similar duplicates, shared-model refactors |
| [Lineage and change impact](lineage-bench.md) | SQLLineage cases and generated lineage pipelines |
| [Dataform preservation](dataform-bench.md) | Protected SQLX text and dependencies |
| [Schema-change compatibility](schema-change-bench.md) | Which models break when a column changes |
| [Whole-pipeline equivalence](pipeline-equivalence.md) | Multi-model refactors |
| [Spider 2.0](spider2-bench.md) | Spider 2.0 BigQuery reference queries as inputs to KumoSQL's analyses |
| [Lineage goldens](lineage-goldens-bench.md) | DataHub and OpenLineage lineage tests, scored against KumoSQL (OpenLineage is the independent oracle) |
