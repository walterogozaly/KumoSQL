# Public SQL evaluation sources for KumoSQL

[Plain-language version](../docs_simple/public-sql-evaluation-sources.md)

Research date: **October 2, 2026** (America/New_York).

This is the research input as written. What KumoSQL already scores, and what is being added from it, is in the [inventory](evals/public-sources.md).

This is a source inventory and proposed import plan. It records primary repositories, released artifacts, documented checker behavior, and license observations. The external suites were not downloaded or executed against KumoSQL during this search. An upstream expected rewrite, an execution check on one database, a bounded solver result, and a universal equivalence proof carry different guarantees.

## Recommended first additions

| Order | Addition | Why it belongs here | First deliverable |
| --- | --- | --- | --- |
| 1 | SQLSolver's Calcite and Spark SQL pairs | Existing SQL-to-SQL pairs with DDL; directly exercises the equivalence APIs | A pinned, attributed subset covering joins, filters, projection, aggregation, CTEs and negative controls |
| 2 | SQLGlot optimizer execution fixtures | Small executable before/afters with NULL and duplicate coverage | Import cases whose upstream harness executes both sides; preserve expected output names and original dialect |
| 3 | Chinook and Northwind | Complete, small relational databases with schema and data | Full schema plus fixed data loaders, row-count checks and multi-table rewrite workloads |
| 4 | DuckDB Jaffle Shop and the Dataform BigQuery example | Complete small analytics pipeline and native SQLX project | Source schemas, compiled SQL snapshots, dependency/lineage expectations and output comparisons |
| 5 | A selected SQLLogicTest corpus | Expected-result regression coverage independent of the SQLGlot optimizer | Small deterministic query/result fixtures with bag versus sequence comparison specified |
| 6 | SQL-RewriteBench | Statement-level rewrite evaluation with case-specific correctness contracts | Parse/prover subset first; preserve contracts and distinguish release claims from locally reproduced checks |
| 7 | Pagila and generated TPC-H | Richer joins/aggregation and scalable analytics | Full database adapters in a slower test lane; fixed generator version/seed/scale |
| 8 | SQLancer-inspired metamorphic cases and Spider test-suite databases | More independent negative evidence and multiple database instances | Deterministic three-valued-logic mutations and counterexample replay |

Orders, subsets, and proposed deliverables are recommendations based on the current repository, not upstream instructions. The detailed inventories below identify the source artifacts and import limitations.

## Current repo coverage and integration limits

The checked-in workbook contains 32 hand-authored `{id, sql_text}` records in `tests/fixtures/sql_subquery_samples.json`. Its shape and exact count are asserted in `tests/test_generic_fixture.py`; keep that fixture intact and add external collections separately. `tests/test_workbook_fixture.py` scores every record against labelled expectations in `tests/fixtures/sql_subquery_samples.expected.json` (strict parse, DuckDB input validity, change, structural removal and verified status, all 32 in the denominator) and runs in the default suite and CI; see [rewrite-rules.md](rewrite-rules.md#authored-fixture-gate).

The existing APIs already provide three useful evaluation lanes:

| Lane | Existing entry points | What to add |
| --- | --- | --- |
| Parsing and rewrite robustness | `apply_rule`, `apply_rules`, `lift_subqueries` | Real queries, SQLX projects, expected no-ops and explicit unsupported features |
| Equivalence and counterexamples | `prove_equivalent`, `prove_equivalent_smt`, `check_result_equivalence` | Paired queries, schemas, positive/negative labels, evidence and assumptions |
| Full databases and pipeline behavior | `execute_on_dataset`, `load_sqlx_project`, `load_compiled_graph`, output-comparison APIs | Complete schemas/data, model graphs, lineage expectations and fixed output snapshots |

All verifier inputs currently assume BigQuery SQL. Local execution translates through SQLGlot to DuckDB. Preserve upstream SQL and its dialect alongside any GoogleSQL adaptation; a successful translation does not establish cross-engine semantic parity.

Synthetic generation supports INT64, FLOAT64, NUMERIC, STRING, BOOL, DATE and TIMESTAMP. It does not enforce primary keys, foreign keys, NOT NULL or CHECK constraints. The SMT API accepts table-to-column names but has no integrity-constraint model. Constraint-dependent rewrites therefore need another checker or an explicitly scoped fixed-database label. ARRAY, STRUCT, JSON, GEOGRAPHY and other unsupported types need separate adapters/native BigQuery coverage.

`QueryOutput` carries column names and values but not result types. Use native BigQuery planning/schema checks to cover types, modes and nested fields. Static rewrite verification does not automatically use SMT; report the two outcomes separately.

The local execution comparator defaults to unordered bags, compares column names case-insensitively and normalizes floats to 12 significant digits (`float_digits=None` compares floats exactly; the policy used is recorded on each result as `float_digits`). Booleans, NaN, arrays and structs are kept apart by type; integers, floats and decimals compare by value. Record the comparison policy per case. Ordered results need `ignore_row_order=False` and a defined tie contract; exact column-name contracts need stricter handling than these defaults. Pipeline fingerprints/checksums provide screening evidence and can collide; use row-level bag comparison for stronger fixed-snapshot confirmation.

Local SQLX execution strips config/JS/pre/post-operation blocks and resolves only literal `ref()` calls. It does not compile Dataform or evaluate incremental branches, `self()`, `when()` or dynamic JavaScript. Use pinned compiled SQL and explicit incremental input state for execution claims; source-block preservation is a different test.

## What counts as a confirmed before/after

| Evidence label | What it establishes | Required record |
| --- | --- | --- |
| `formal_proof` | Equivalence under the prover's modeled semantics and stated assumptions | Tool/version, successful result or proof artifact, schema, constraints and assumptions |
| `bounded_check` | No counterexample within the checked bound | Bound, solver/version, timeout and supported features; do not call it an all-database proof |
| `native_engine_regression` | The source test's expected native-engine behavior | Engine/version, schema/data, query and expected result; a confirmed pair requires both sides executed on the same setup |
| `fixed_database_results` | Matching outputs on specified database snapshots | Snapshot checksum, comparison semantics, outputs and engine/version |
| `randomized_results` | Matching outputs on tested seeds/instances | Generator/version, seeds, sizes and comparison policy |
| `golden_rewrite` | The upstream optimizer expects this output | Source test and expected SQL; equivalence still needs a separate check |
| `author_claim` / `unverified` | A claim or proposed rewrite exists | Source and limitation; do not promote it to equivalence gold |

Record proof and execution evidence independently. Keep known non-equivalent pairs and supplied counterexamples: avoiding false proofs is more important here than accepting every positive pair. A counterexample must be replayed in the appropriate native engine when feasible, especially when casts, collations, floating point or integrity constraints matter.

## Evaluation suites and reusable oracles

P0 means an immediate small import; P1 means the next corpus or a separate engine job; P2 means later coverage or an external checker. Restricted/reference entries are useful research sources but not default vendoring recommendations. License entries describe the inspected source; constituent datasets and copied third-party files can have different terms.

| ID / priority | Source and concrete artifacts | Oracle and fit | Import limits |
| --- | --- | --- | --- |
| E01 / P0 | [SQLSolver](https://github.com/SJTU-IPADS/SQLSolver): [Calcite pairs](https://github.com/SJTU-IPADS/SQLSolver/blob/main/sqlsolver_data/calcite/calcite_tests), [Spark pairs](https://github.com/SJTU-IPADS/SQLSolver/blob/main/sqlsolver_data/db_rule_instances/spark_tests), [Calcite DDL](https://github.com/SJTU-IPADS/SQLSolver/blob/main/sqlsolver_data/schemas/calcite_test.base.schema.sql) | Alternating lines are explicitly declared equivalent pairs. Four families: Calcite, Spark, TPC-C, TPC-H. Independent prover can cross-check imported cases. | [Apache-2.0](https://github.com/SJTU-IPADS/SQLSolver/blob/main/LICENSE); Calcite-compatible parsing. Preserve line IDs, DDL and constraints. Pair declaration alone does not supply a newly reproduced proof. NEQ is not always a definitive inequivalence result according to the API documentation. |
| E02 / P0-P1 | [SQL-RewriteBench](https://github.com/SQL-RewriteBench/benchmark): [inputs](https://github.com/SQL-RewriteBench/benchmark/tree/main/benchmark/benchmark_inputs%20packages/cases), [references](https://github.com/SQL-RewriteBench/benchmark/tree/main/benchmark/reproducibility/reference_bundle/cases), [specification](https://github.com/SQL-RewriteBench/benchmark/blob/main/docs/benchmark_specification.md) | Release declares 180 cases: EQUIV 48, PERF 44, ROBUST 58, PG-AWARE 30; SQL, schema profiles, reference targets, checker contracts and evidence. Strong statement-level evaluation design. | [Apache-2.0 with exclusions](https://github.com/SQL-RewriteBench/benchmark/blob/main/THIRD_PARTY_NOTICES.md). PostgreSQL reproduction requires external [TPC-DS SF10/DSB setup](https://github.com/SQL-RewriteBench/benchmark/blob/main/docs/dataset_setup.md). Individual case files returned cache misses during this search; validate contracts/results on ingestion. |
| E03 / P0 | [DuckDB SQLLogicTests](https://github.com/duckdb/duckdb/tree/main/test/sql), [format](https://duckdb.org/docs/current/dev/sqllogictest/overview), [Python runner](https://github.com/duckdb/duckdb-sqllogictest-python) | Self-contained DDL, inserts, SQL and expected results. Start with CTE, subquery, join, conjunction, set operations and aggregation. | [MIT](https://github.com/duckdb/duckdb/blob/main/LICENSE), subject to file-specific provenance. Preserve test state/setup; filter engine commands. Python runner is experimental. Recompute strict row-bag comparisons rather than inheriting permissive SLT formatting. |
| E04 / P1 | [Original SQLLogicTest](https://www.sqlite.org/sqllogictest/doc/trunk/about.wiki), [test files](https://www.sqlite.org/sqllogictest/dir?name=test) | Portable expected-result scripts and labels relating query results. Broad independent regression oracle. | Exact artifact license still needs inspection. `valuesort` loses row grouping; real values are formatted to three decimals; hashes hide exact output. None of those modes is a sufficient strict rewrite-equivalence oracle. |
| E05 / P0-P1 | [SQLancer](https://github.com/sqlancer/sqlancer), [oracle/support matrix](https://github.com/sqlancer/sqlancer/blob/main/CONTRIBUTING.md) | Generates schemas, data, queries and reproducible logs using TLP, NoREC, PQS and other metamorphic oracles. Particularly useful for NULL logic and negative cases. | [MIT](https://github.com/sqlancer/sqlancer/blob/main/LICENSE.md). Generator, not a fixed paired dataset. Use a bounded native DuckDB/SQLite job and commit reduced deterministic DDL/data/query regressions. |
| E06 / P1 | [Spider distilled test suites](https://github.com/taoyds/test-suite-sql-eval), [generation code](https://github.com/ruiqi-zhong/TestSuiteEval) | Multiple database instances reduce accidental agreement on one database; queries, schemas and execution comparison. | [Evaluation code Apache-2.0](https://github.com/taoyds/test-suite-sql-eval/blob/master/LICENSE); dataset terms separate. Preserve literal values and DISTINCT (`--keep_distinct`); text-to-SQL defaults can weaken the output contract. |
| E07 / P1 | [Spider original](https://github.com/taoyds/spider), [official release](https://yale-lily.github.io/spider) | Query strings, `tables.json` with keys/types, populated SQLite databases. Diverse real schemas and source workloads. | Repository Apache-2.0; inspect database terms. Use corrected official train/dev data. Source queries are not confirmed rewrite pairs; generate KumoSQL targets and check them. |
| E08 / P1 | [Spider 2.0](https://github.com/xlang-ai/Spider2): [released gold SQL](https://github.com/xlang-ai/Spider2/tree/main/spider2-lite/evaluation_suite/gold/sql), [database resources](https://github.com/xlang-ai/Spider2/tree/main/spider2-lite/resource/databases) | Current Lite README lists 547 tasks: 214 BigQuery, 198 Snowflake, 135 SQLite. DBT variant lists 68 DuckDB tasks/projects. Good complex SQL and graph workloads. | [MIT repo](https://github.com/xlang-ai/Spider2/blob/main/LICENSE), external data terms separate. Only some gold SQL is public. Import released cases only; do not imply full benchmark labels or local access to every database. |
| E09 / P1-P2 | [BIRD Mini-Dev](https://github.com/bird-bench/mini_dev), [canonical dataset card](https://huggingface.co/datasets/birdsql/bird_mini_dev) | Three 500-case SELECT dialect splits over eleven databases: SQLite, MySQL, PostgreSQL. Populated databases and gold SQL. | Card says CC BY-SA 4.0; retain attribution and inspect constituent data terms. Non-SQLite versions are adapted/refined. Use strict equality, not soft F1; execute each source dialect independently. |
| E10 / optional | [VeriEQL](https://github.com/VeriEQL/VeriEQL): [Calcite JSONL](https://github.com/VeriEQL/VeriEQL/blob/main/benchmarks/calcite/calcite2.jsonlines), [literature JSONL](https://github.com/VeriEQL/VeriEQL/blob/main/benchmarks/literature/literature-rewrite.jsonlines) | README lists 397 Calcite, 64 literature and 23,224 LeetCode pairs; schemas/constraints and bounded counterexample tools. Valuable positive and negative evidence. | [CC BY-NC-SA 4.0](https://github.com/VeriEQL/VeriEQL/blob/main/license.md). Use an optional external corpus with compatible terms. LeetCode ancestry needs separate attention. Bounded checked/not-refuted results are not universal proofs. |
| E11 / P2 | [ParSEval](https://github.com/sfu-db/ParSEval), `data/`, `instantiate_db`, `disprove` | Plan-aware database generation and counterexample search across SQLite/MySQL/PostgreSQL. Complements unconstrained random rows. | [Apache-2.0](https://github.com/sfu-db/ParSEval/blob/main/LICENSE). Freeze distinguishing instances. README notes aggregation-DISTINCT limitations/false positives; generated-instance agreement remains execution evidence. |
| E12 / P2 | [QED prover](https://github.com/qed-solver/prover), [parser](https://github.com/qed-solver/parser), `tests/calcite/` | Calcite-derived query-pair IR; parser can consume DDL and two SQL queries. Useful independent prover integration. | Prover MIT, parser Apache-2.0. IR is not directly a SQL pair corpus. README treats aggregates and ORDER BY/LIMIT as uninterpreted; retain that semantic limit. |
| E13 / P2 | [Logos corpus/provenance](https://github.com/WindOctober/Logos/blob/main/benchmarks/core/README.md) | Includes real-commit pairs and newly generated deterministic rewrite targets, manifests and a Rocq proof workflow. Useful provenance and counterexample models. | Logos-authored code MIT; source-family licenses vary. Upstream R-Bot `_0` and `_1` files are independent parameterized inputs, not before/afters. Generated targets are not assumed equivalent; require per-case evidence. |
| E14 / P2 | [PARROT](https://github.com/OpenDataBox/PARROT), `benchmark/`, `validator/` | Cross-system SQL translation and native parsers/execution checks. README distinguishes 598 curated pairs from a larger syntax collection. Good long-tail dialect discovery. | README claims MIT, but linked LICENSE file was absent when fetched; constituent corpora retain their terms. Not same-dialect rewrite gold. Import from original sources and verify native results. |
| E15 / research | [Feedback-driven SQL optimization artifact](https://github.com/KostovMartin/mk-feedback-driven-sql-optimization), `experiment-artifacts/`, `experiment-results/` | PostgreSQL candidate rewrites with paired execution measurements, provenance and raw run-data bundles. Useful example of conservative promotion and reproducibility. | GPL-3.0-or-later source; third-party workloads/data separate. Empirical validation rather than formal equivalence. Link/reference first; inspect exact retained pair and run artifact before importing. |

Also inspected [E3-Rewrite](https://arxiv.org/html/2508.09023v1) and [QUITE](https://github.com/Yuyang-Song/QUITE). E3-Rewrite's paper describes executable/equivalent/efficient objectives, but this search did not establish a public licensed pair release. QUITE documents checking original/rewrite outputs on a benchmark instance; that is fixed-database evidence. Neither should be promoted wholesale to universal equivalence gold. LLM-R2/R-Bot query sets are useful workloads, but a filename variant or generated target does not establish a confirmed transformation.

## Complete publicly defined database candidates

These sources expose an entire schema plus data, data loading, or a generator. Hosted datasets and data-rights-limited sources are labeled separately. Sizes/counts below are upstream documentation, not local measurements.

| ID / priority | Source and full database artifacts | Coverage and execution path | Rights / caveats |
| --- | --- | --- | --- |
| D01 / P0 | [Jaffle Shop DuckDB](https://github.com/dbt-labs/jaffle_shop_duckdb): [seeds](https://github.com/dbt-labs/jaffle_shop_duckdb/tree/duckdb/seeds), [models](https://github.com/dbt-labs/jaffle_shop_duckdb/tree/duckdb/models) | Three raw tables: customers/orders/payments, staging and analytics models, YAML tests. Compile templates and load all CSVs into DuckDB. | Apache-2.0; use the `duckdb` branch. The separate modern `dbt-labs/jaffle-shop` had an [unresolved license issue](https://github.com/dbt-labs/jaffle-shop/issues/111); do not inherit dbt-core's license. |
| D02 / P0 | [Chinook](https://github.com/lerocha/chinook-database): [DataSources](https://github.com/lerocha/chinook-database/tree/master/ChinookDatabase/DataSources), [v1.4.5](https://github.com/lerocha/chinook-database/releases/tag/v1.4.5) | Full relational music-store schema/data; SQLite script/database, PostgreSQL script, JSON and XSD. Small business joins, hierarchy, NULLs and aggregation. | [MIT-style grant](https://github.com/lerocha/chinook-database/blob/master/LICENSE.md). Pin script/release. Keep upstream and adapted BigQuery DDL separately. |
| D03 / P0-P1 | [Northwind original](https://github.com/microsoft/sql-server-samples/tree/master/samples/databases/northwind-pubs): [instnwnd.sql](https://github.com/microsoft/sql-server-samples/blob/master/samples/databases/northwind-pubs/instnwnd.sql) | Full create/load script; orders/details/customers/products and self-joins. Compact second business database with different relationships. | [Microsoft sample MIT](https://github.com/microsoft/sql-server-samples/blob/master/license.txt); original is old T-SQL. Prefer original provenance over an unverified port and validate adapted row counts. |
| D04 / P1 | [Pagila](https://github.com/xzilla/pagila): `pagila-schema.sql`, `pagila-data.sql`, `pagila-insert-data.sql`; [alternate maintained lineage](https://github.com/devrimgunduz/pagila) | Rental database with joins, views, functions, dates and PostgreSQL features. INSERT data is easier to adapt than COPY; keep native feature cases separately. | PostgreSQL License. Pin one fork/version; JSONB/temporal variants differ. Shares ancestry with Sakila. |
| D05 / P1 | [Sakila official distribution](https://dev.mysql.com/doc/sakila/en/sakila-installation.html): `sakila-schema.sql`, `sakila-data.sql` | Full MySQL tables, seed data, views/procedures/triggers. Useful original-dialect baseline; official verification documents 1,000 films. | [New BSD grant applies to the two SQL files](https://dev.mysql.com/doc/sakila/en/sakila-license.html); distribution documentation has different terms. Version-sensitive SQL/spatial behavior. |
| D06 / P1 | [TPC-H via DuckDB](https://duckdb.org/docs/current/core_extensions/tpch): `dbgen`, `tpch_queries`, `tpch_answers`; [tpcgen-rs](https://github.com/datafusion-contrib/tpcgen-rs) | Eight-table analytical schema, scalable generated data, 22 queries; small scales fit slower CI. Pin generator/seed/scale and expected-answer version. | Current Rust generator [Apache-2.0](https://github.com/datafusion-contrib/tpcgen-rs/blob/main/LICENSE). DuckDB's [dbgen subtree has TPC terms](https://github.com/duckdb/duckdb/blob/main/extension/tpch/dbgen/LICENSE); query/answer terms need separate provenance. Tests are not certified TPC results. |
| D07 / P2 | [TPC-DS via DuckDB](https://duckdb.org/docs/current/core_extensions/tpcds), [generator/schema/query/answer files](https://github.com/duckdb/duckdb/tree/main/extension/tpcds/dsdgen); [Rust alternative](https://github.com/datafusion-contrib/tpcgen-rs) | Complex warehouse schema, generated data and 99 queries, including CTEs, grouping and windows. Separate native full-scale and portable subsets. | Generator and reference-artifact terms differ. Pin benchmark and generator versions: changed generator eras can change answers. |
| D08 / P2 | [Microsoft DSB](https://github.com/microsoft/dsb): `scripts/generate_dsb_db_files.py`, PostgreSQL/SQL Server loaders, query templates | TPC-DS-derived data with correlations and dynamic workload variation. Good skew/correlation stress beyond independent random values. | Reference TPC-derived subtree terms require inspection. README requires all tables generated in dependency order to retain correlation; not a certified TPC-DS run. |
| D09 / P2 | [AdventureWorks / DW](https://github.com/microsoft/sql-server-samples/tree/master/samples/databases/adventure-works), [releases](https://github.com/microsoft/sql-server-samples/releases/tag/adventureworks) | OLTP and star-schema warehouse; inspect `oltp-install-script/instawdb.sql`, warehouse scripts and CSVs. Rich complete schemas and long joins. | Microsoft sample MIT; substantial SQL Server/FILESTREAM adaptation. Pin edition deliberately; 2025 data dates differ. Prefer inspectable scripts/data over Git binary backups. |
| D10 / P2 | [WideWorldImporters / DW](https://github.com/microsoft/sql-server-samples/tree/master/samples/databases/wide-world-importers), [v1.0 release](https://github.com/microsoft/sql-server-samples/releases/tag/wide-world-importers-v1.0) | Full SSDT schemas, samples/workload drivers and full database backups. Enterprise OLTP/OLAP, temporal/IoT coverage. | Microsoft source MIT; SQL Server environment and multi-GB restores make it an optional enterprise lane. |
| D11 / P2 | [Employees/test_db](https://github.com/datacharmer/test_db): `employees.sql`, `load_*.dump`, integrity-check SQL | Full salary/history database; documented 300,024 employees and 2,844,047 salary rows. Medium-size temporal and composite-key tests. | [CC BY-SA 3.0](https://dev.mysql.com/doc/employee/en/employees-license.html). Keep attribution/share-alike attached to imported SQL/data; larger than a PR smoke fixture. |
| D12 / P2 | [Postgres Professional Airlines](https://postgrespro.com/community/demodb), [2025-09-01 3m dump](https://edu.postgrespro.ru/demo-20250901-3m.sql.gz) | Nine tables with views, JSONB, arrays, temporal ranges and time zones; complete dumps and generator. Smallest current dump documented at 133 MB compressed/~1.3 GB installed. | Current version MIT; older editions differ. PostgreSQL 15+; freeze version/locale/time zone. Adapted small subsets should be labeled reductions. |
| D13 / optional | [Join Order Benchmark/IMDb](https://github.com/gregrahn/join-order-benchmark): `schema.sql`, `fkindexes.sql`, queries and original-data links | High-value many-table joins, skew and cardinality stress. Pair with the exact historical data snapshot. | Explicit repo license not verified; IMDb terms separate. README warns alternate frozen datasets change results. Catalog/fetch externally pending rights; do not vendor rows by assumption. |
| D14 / native | [BigQuery Stack Overflow](https://docs.cloud.google.com/bigquery/docs/best-practices-performance-nested), `bigquery-public-data.stackoverflow` | Hosted relational tables for joins, grouping and nested-layout experiments. Snapshot all table metadata and relevant partitions/data. | Project/access and query charges. [Contribution licenses vary by date](https://stackoverflow.com/help/licensing); inspect dataset-specific terms. No live catalog/row-count API audit was performed. |
| D15 / native | [GA4 obfuscated ecommerce sample](https://developers.google.com/analytics/bigquery/web-ecommerce-demo-dataset), `bigquery-public-data.ga4_obfuscated_sample_ecommerce.events_*` | Official sample dated 2020-11-01 through 2021-01-31; complete documented event schema with nested/repeated fields. Start with metadata and authored synthetic rows. | Explicit source-row redistribution grant not verified; public query access is distinct. Obfuscation limits internal consistency. Needs new nested-type support/native engine. |
| D16 / future | [Apache Sedona SpatialBench](https://github.com/apache/sedona-spatialbench): generator, Arrow/CLI/query packages, `benchmark/answers/` | Synthetic full spatial schema, Parquet generation, cross-engine queries and reference answers. Future GEOGRAPHY/ST_* coverage. | Apache-2.0 with NOTICE; BigQuery adapter not listed, so GoogleSQL parity is new work. |

For small analytical CI, prefer D01-D03 first, then one of D04/D05 and D06. Use D15-shaped **authored synthetic** data to cover native nested schemas without depending on redistribution of hosted rows. Large databases should use reproducible manifests/downloads or generation, rather than committed backups.

## SQL transformation before/after sources

These entries distinguish actual SQL pairs from rule/proof descriptions and physical plans. E01/E02/E10 already provide pair-oriented collections; the additional sources below broaden exact transformations and semantic boundaries.

| ID / priority | Source artifact | What is actually available | Appropriate confirmation level |
| --- | --- | --- | --- |
| R01 / P0 | [SQLGlot optimizer fixtures](https://github.com/tobymao/sqlglot/tree/main/tests/fixtures/optimizer), [test harness](https://github.com/tobymao/sqlglot/blob/main/tests/test_optimizer.py) | SQL inputs followed by expected SQL for simplification, CTE elimination, projection/predicate pushdown, joins and full optimizer output. | [MIT](https://github.com/tobymao/sqlglot/blob/main/LICENSE). Some tests only compare golden SQL; `test_optimize` executes full-optimizer fixtures with DuckDB. Import the harness's execution-backed subset first and preserve setup. Because KumoSQL uses SQLGlot, also require an independent execution/negative-case oracle. |
| R02 / P0 | SQLSolver E01 and its [artifact repository](https://github.com/SJTU-IPADS/SQLSolver-artifacts) | Real SQL-to-SQL pairs plus schemas; reproduction scripts for multiple verifiers and discovered rules. | Upstream equivalent-pair declarations, with independent solver reproduction available. Import constraints and record checker result/version; preserve unknown/timeouts rather than forcing labels. Artifact subprojects retain their own licenses. |
| R03 / P1 | [WeTune](https://github.com/WeTune/WeTune-code), [verified paired file](https://github.com/winoros/wetune/blob/main/wtune_data/calcite/calcite_tests), [extraction patch](https://github.com/winoros/wetune/blob/main/wtune_data/calcite/calcite.patch) | The inspected paired file has 464 SQL lines: 232 consecutive pairs. Includes application schemas/workloads and rule verification/reproduction scripts. | [Apache-2.0 on the inspected tree](https://github.com/winoros/wetune/blob/main/LICENSE). Pin the exact tree; some pairs need Calcite catalog constraints or contain printer/pseudo-SQL artifacts. Generated TSV output names do not establish committed downloadable targets. |
| R04 / optional | [QueryBooster Tableau/TPC-H experiments](https://github.com/ISG-ICS/QueryBooster/blob/main/experiments/tpch_pg.md), [WeTune CSV](https://github.com/ISG-ICS/QueryBooster/blob/main/experiments/Test_wetune.csv), [Calcite CSV](https://github.com/ISG-ICS/QueryBooster/blob/main/experiments/calcite_tests.csv) | Concrete human-authored SQL rewrite examples and experimental pair collections, including PostgreSQL BI-style SQL. | GPL-3.0 repo; use as an optional/reference corpus with compatible import terms. Measurements and examples do not prove arbitrary database equivalence; preserve constraints and re-execute. |
| R05 / reference | [Apache Calcite optimizer regression XML](https://github.com/apache/calcite/blob/main/core/src/test/resources/org/apache/calcite/test/RelOptRulesTest.xml) | SQL plus `planBefore` and `planAfter`, optimizer assumptions and rule cases. | Apache-2.0. This is chiefly SQL-to-plan evidence, not a direct SQL-to-SQL corpus. Prefer SQLSolver's extraction; generating SQL from plans requires its own semantic validation. |
| R06 / P0-P1 | [Cosette examples](https://github.com/uwdb/Cosette/tree/master/examples), [historical labels](https://github.com/uwdb/Cosette/blob/master/examples/calcite/calcite_result_with_label.csv), [HoTTSQL optimizations](https://github.com/uwdb/Cosette/tree/master/hott/optimizations) | `.cos` files embed schemas, queries and verification directives; Coq theorems provide another evidence source. Positive and known unequal cases are especially useful. | [BSD-2-Clause](https://github.com/uwdb/Cosette/blob/master/LICENSE.txt). Distinguish proof certificates from bounded counterexample search. Earlier semantic models omit native NULL behavior; translate supported cases with explicit assumptions. |
| R07 / P2 | [Logos core corpus](https://github.com/WindOctober/Logos/blob/main/benchmarks/core/README.md) | Real-commit pairs plus newly generated rewrite pairs with explicit provenance; formal workflow and source-family metadata. | Import only per-case checked, compatible families. `_0`/`_1` parameter variations are not proof of a relationship. Track overlapping Calcite/WeTune ancestry. |
| R08 / native research | [Google BigQuery computation guide](https://docs.cloud.google.com/bigquery/docs/best-practices-performance-compute), [nested guide](https://docs.cloud.google.com/bigquery/docs/best-practices-performance-nested) | Documented query examples and measured layout changes; useful optimization hypotheses. | Documentation recommendations or fixed-instance measurements. Exclude task-changing approximate/projection/top-k rewrites from equivalence positives. Nested-layout cases need separate schema/data mapping contracts. |
| R09 / P1 | [SPES](https://github.com/georgia-tech-db/spes), [paired Calcite JSON](https://github.com/georgia-tech-db/spes/blob/main/testData/calcite_tests.json) | Named `q1`/`q2` SQL pairs and bag-equivalence checker with NULL-aware predicate reasoning. | Apache-2.0. A pair's presence is not a proof result, and output ordering is outside its bag contract. Substantial overlap with other Calcite-derived collections. |

### Concrete cases to extract first

1. **NULL-safe Boolean simplification.** The [SQLGlot simplify fixture](https://github.com/tobymao/sqlglot/blob/main/tests/fixtures/optimizer/simplify.sql) contains this exact short pair:

   ```sql
   -- before
   SELECT t_bool.a AND TRUE FROM t_bool;
   -- after
   SELECT t_bool.a FROM t_bool;
   ```

   It is a useful three-valued-logic value test. As written, unaliased expression column names can differ between engines. Preserve the source case; for a KumoSQL result-schema contract, create a separately labeled adaptation with the same explicit alias on both projections. The fixture pair is a golden rewrite; check whether that exact case is executed upstream before assigning an execution label.

2. **Predicate movement with join boundaries.** [SQLGlot predicate-pushdown fixtures](https://github.com/tobymao/sqlglot/blob/main/tests/fixtures/optimizer/pushdown_predicates.sql) include transformations and retained predicates around joins. Import both successful pushdowns and intentional no-ops for RIGHT/FULL/outer joins. Add unmatched and NULL join rows; outer-join filtering is a frequent source of false equivalence.

3. **Projection/subquery normalization.** SQLSolver's documented [example/API and benchmark pairs](https://github.com/SJTU-IPADS/SQLSolver) supply direct projections versus derived-table projections and broader Calcite/Spark instances. Retain the original result-column naming contract: KumoSQL may correctly refuse a bag-equivalent value query whose names differ. Do not copy an API illustration into an unconditional schema-equality positive.

4. **Concrete Calcite-derived pairs from WeTune.** In the [inspected 232-pair file](https://github.com/winoros/wetune/blob/main/wtune_data/calcite/calcite_tests), pair 19 (SQL lines 37-38) removes a GROUP BY key fixed by an equality filter; pair 170 (339-340) removes an IS NOT NULL predicate implied by equality; pair 130 (259-260) turns a LEFT JOIN into an INNER JOIN under a null-rejecting filter and pushes the filter down. These are published before/afters; keep source schema and query text intact and add native adversarial executions before assigning a stronger evidence label. Do not blanket-import the whole file as gold: other entries contain empty-VALUES pseudo-SQL, alias artifacts, nested aggregates or constraint-sensitive COUNT DISTINCT changes.

5. **Constraint-dependent join elimination.** Select a SQLSolver/WeTune case with its original DDL, uniqueness and foreign-key assumptions. Store the positive case under those constraints and a negative sibling that removes the guarantee. Until constraint-aware checking exists, evaluate natively on constrained databases or expect the current SMT/AST checker to refuse it.

6. **CTE and repeated-work rewrites in real pipelines.** Use R01/E01 cases for statement pairs and the native Dataform projects below for pipeline refactors. The source model and a materialized replacement can agree only when the same input snapshot, interpolation/configuration and incremental boundaries are used.

7. **Self-product elimination under DISTINCT.** [Cosette's SelfJoin0 case](https://github.com/uwdb/Cosette/blob/master/examples/sqlrewrites/SelfJoin0.cos) supplies this short pair:

   ```sql
   -- before
   select distinct x.* from r x, r y
   -- after
   select distinct x.* from r x
   ```

   DISTINCT makes this identity safe for multiplicities, including empty input. Keep a negative sibling that removes DISTINCT. Also extract [Cosette's unequal COUNT example](https://github.com/uwdb/Cosette/blob/master/examples/inequal_queries/countbug.cos): replacing a correlated count with a grouped inner join loses zero-count rows. Preserve the source's evidence and assumptions rather than treating every DSL case as a native BigQuery proof.

[R-Bot's canonical repository](https://github.com/curtis-sun/LLM4Rewrite) provides Apache-2.0 code, workload DDL and Calcite rewrite reproduction. Its timing/rule-sequence logs are not automatically SQL targets. Prefer exact regenerated pairs with manifests, such as [Logos' R-Bot rewrite manifest](https://github.com/WindOctober/Logos/blob/main/benchmarks/core/rbot/rewrite-pairs.manifest.json), and independently confirm each target.

Do not mark approximate COUNT/DISTINCT replacements, changed LIMIT or ORDER BY, early top-k joins, or tie-sensitive aggregate/window changes as equivalent solely because a performance article recommends them. These belong in conditional, negative or explicitly task-changing cases.

## BigQuery and Dataform projects

These are real workload/graph sources. They do not automatically supply known-equivalent before/after pairs or self-contained source data.

| Source | Verified public artifacts | KumoSQL use and limitations |
| --- | --- | --- |
| [Dataform BigQuery example](https://github.com/dataform-co/dataform-example-project-bigquery) | `definitions/sources`, `definitions/staging`, `definitions/reporting`, `workflow_settings.yaml`; [MIT license](https://github.com/dataform-co/dataform-example-project-bigquery/blob/master/LICENSE) | Best initial native SQLX project for ref masking, config preservation, graph loading and lineage. Pin compiler and source schemas; check source-data availability before calling it a full database fixture. |
| [BigQuery Utils](https://github.com/GoogleCloudPlatform/bigquery-utils) | SQL/UDFs, scripts, views and [Dataform assertion tests](https://github.com/GoogleCloudPlatform/bigquery-utils/tree/master/dataform/examples/dataform_assertion_unit_test); [Apache-2.0](https://github.com/GoogleCloudPlatform/bigquery-utils/blob/master/LICENSE) | Native GoogleSQL syntax and independently expected scalar/UDF results; Data Vault and UDF projects broaden SQLX coverage. UDF expected values are not query rewrite pairs. Select executable fixtures with locally defined inputs. |
| [Marketing Analytics Jumpstart Dataform](https://github.com/GoogleCloudPlatform/marketing-analytics-jumpstart-dataform) | `definitions`, `includes`, source declarations and base/product model layers; [Apache-2.0](https://github.com/GoogleCloudPlatform/marketing-analytics-jumpstart-dataform/blob/main/LICENSE) | GA4 and Ads pipelines exercise nested fields, wildcard sources, incremental branches and lineage. The old marketing-data-engine URL redirects here. External exports must be supplied; use public/synthetic input snapshots. |
| [Security Analytics Dataform](https://github.com/GoogleCloudPlatform/security-analytics/tree/main/dataform) | Log-source declaration, hourly/daily summaries, lookup/stat/report models; [Apache-2.0](https://github.com/GoogleCloudPlatform/security-analytics/blob/main/LICENSE) | Larger incremental and temporal pipeline. Repository is archived; use a fixed revision. Cloud Logging inputs are environment-specific, so it is a project fixture rather than a public data dump. |
| [ClickBench BigQuery adapter](https://github.com/ClickHouse/ClickBench/tree/main/bigquery) | [Full table DDL](https://github.com/ClickHouse/ClickBench/blob/main/bigquery/create.sql), [43 query workload](https://github.com/ClickHouse/ClickBench/blob/main/bigquery/queries.sql), load and timing scripts | Excellent native GoogleSQL parser/performance workload over a wide table; many cases use LIMIT/ORDER BY and should test conservative refusal. Current [license is CC BY-NC-SA 4.0](https://github.com/ClickHouse/ClickBench/blob/main/LICENSE), so treat it as an optional restricted corpus. Queries/timings are not rewrite labels. |

## BigQuery public data and documented optimizations

[BigQuery's public dataset program](https://docs.cloud.google.com/bigquery/public-data) provides hosted tables; query execution requires an appropriate project and can incur charges. Public access does not imply a universal redistribution license or a frozen dataset. Native execution should be opt-in, bounded and tied to exact snapshots/partition ranges.

Start with Stack Overflow for joins and aggregation, GA4's obfuscated sample for ARRAY/STRUCT/UNNEST, and one partitioned time-series dataset for pruning and date filters. The [GA4 export schema](https://support.google.com/analytics/answer/7029846) supplies field-level definitions; snapshot actual table metadata instead of assuming every export has identical optional fields.

Google's [computation optimization guide](https://docs.cloud.google.com/bigquery/docs/best-practices-performance-compute) is useful for transformation hypotheses, especially pre-aggregation and repeated-work avoidance. Its examples can also change projection, limits or tie behavior. Record required key constraints and recheck complete output schemas/results before turning a recommendation into a gold pair.

The [nested-field guide](https://docs.cloud.google.com/bigquery/docs/best-practices-performance-nested) supplies a flat Stack Overflow representation and an ARRAY<STRUCT> representation with documented queries and measurements. This belongs in a **physical-layout/pipeline migration** suite because the schema and data representation change. Keep it separate from same-schema statement equivalence.

## Proposed repository layout

This layout is a proposed implementation; the directories and runner below have not been added.

```text
tests/fixtures/public_sql/
  manifest.json
  licenses/
  pairs/<corpus>/<case>/{before.sql,after.sql,schema.json,evidence.json}
  workloads/<corpus>/<case>/{source.sql,adapted.bigquery.sql,case.json}
  databases/<database>/{schema.sql,schema.json,data/,manifest.json}
  projects/<project>/{source/,compiled_graph.json,expected_lineage.json}
tools/import_public_sql.py
tools/run_public_sql_evals.py
```

Use a case manifest with these fields, adding only those relevant to its lane:

```text
id, corpus_id, source_url, source_repository, source_revision, source_paths,
retrieved_at, license, license_url, redistribution_status, checksums,
original_dialect, target_dialect, source_sql_path, adapted_sql_path,
before_sql_path, after_sql_path, transformation_family,
database_id, ddl_path, schema_path, data_paths,
expected_relation, evidence_kind, evidence_url, evidence_revision,
constraints_required, constraints_supported, assumptions,
comparison_mode, check_column_names, check_column_types, float_policy,
generator_version, seeds, scale_factor, expected_verifier_outcomes,
feature_tags, unsupported_reason, split, size_tier, timeout_ms
```

Pin immutable revisions/releases and file hashes at import time. Keep source case IDs and notices. Store adaptations separately with an explicit change record. Do not deduplicate only by raw SQL text: several cases may differ in schema constraints, NULL assumptions or comparison semantics. Also track shared ancestry; Calcite-derived collections in different papers are not independent evaluation sets.

Complete database fixtures need all tables, types and relationships, relevant views/functions/triggers, loader order, fixed data or a reproducible generator, and checks of table/row counts and constraints. Preserve decimal precision/scale, collations, locale and time zone; cross-engine adaptations can change them. Loading only the referenced columns of a few tables is a useful reduced fixture, but should be labeled as a reduction. Add adversarial NULL/duplicate/unmatched/empty cases separately without violating the original database's declared constraints.

## Evaluation and CI plan

1. **PR smoke:** small pinned/attributed pairs and full small databases; all rule outputs checked; known negative pairs replayed; current SQLGlot version matrix retained. Include representative SQLX graphs and byte-for-byte no-ops.
2. **Full local/native job:** all small corpora and database loaders; complete pair coverage with structured results and explicit unsupported/error categories. Keep source-engine execution separate from BigQuery-to-DuckDB execution.
3. **Nightly:** larger synthetic benchmarks, expanded metamorphic/random tests and time-bounded SMT; minimize failing cases and promote them to small regressions.
4. **Opt-in BigQuery:** native planning and output schema checks, then bounded execution on fixed snapshots/partitions with maximum-bytes controls. Larger performance experiments belong here or in a dedicated native-engine job.

Report strict-parse coverage and recovery rate; changed-query rate per rule; proofs, counterexamples, unsupported cases, timeouts and execution errors; positive proof coverage within the documented subset; and false proofs on negative cases. Count no-ops and skipped cases in the full denominator. Require zero false proofs on established negatives.

Measure column names/types/order, bag versus sequence behavior, NULLs/duplicates, solver/runtime p50/p95, and data/query size. For pipeline fixtures also track unresolved sources, unknown columns, dependency order and lineage expectations. Publish structural complexity separately from measured runtime/cost. A smaller AST, lower planner estimate or successful dry run does not establish a faster or equivalent query.

## Sources to keep optional or defer

- Restrictive or incomplete licensing: VeriEQL, ClickBench, inherited IMDb data, TPC generator subtrees, and corpora without an inspected license grant. Preserve source-specific terms instead of inheriting a parent project's license.
- Paper-only releases or inaccessible artifacts: retain the citation and clearly mark the gap; do not invent a downloadable pair corpus.
- Optimizer plan snapshots: valuable rule evidence, but require SQL regeneration and independent checking to become SQL-to-SQL cases.
- LLM-generated candidate rewrites: import only with per-pair checker evidence, schema/data and output contracts.
- Performance recommendations that alter the task: approximate aggregates, added filters, changed projection, LIMIT/top-k changes and unsupported uniqueness assumptions are not unconditional equivalence positives.

DBridge was considered, but its principal scope is [imperative-program-to-SQL optimization](https://www.cse.iitb.ac.in/infolab/DBridge/DBridge.html). EQUITAS is a useful methodological comparator, but this search did not locate a reusable primary licensed corpus beyond the overlapping query families above.

The immediate implementation target should be a manifest-driven small corpus and full-database loader, followed by source importers. That creates reusable evaluation infrastructure before large or credential-dependent workloads are introduced.
