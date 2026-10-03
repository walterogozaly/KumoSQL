# Public SQL evaluation sources: inventory

[Public SQL evaluation sources](../public-sql-evaluation-sources.md) (research dated 2026-10-02) lists 50 public suites, databases, rewrite corpora and BigQuery projects. This page records, for each one, whether KumoSQL already scores it, what it overlaps, its licence, whether it can be downloaded from here, and what is being added. It was checked on 2026-10-03 by cloning every repository at its current head; the commit is the pin for anything added.

Downloads go through a proxy that blocks HuggingFace, Google Drive, Dropbox, Git LFS, Aliyun OSS, `yale-lily.github.io`, `downloads.mysql.com`, `postgrespro.ru`, `duckdb.org` and `docs.cloud.google.com`. GitHub clones, raw files and release assets work.

Status: **covered** (an existing eval already scores it), **new** (being added, one pull request per source), **not added** (with the reason). Sources without a licence, or with GPL or share-alike terms, are downloaded at a pinned commit when the eval runs and never committed.

## Evaluation suites (E01–E15)

| ID | Source | Licence | Download | Size | Status |
| --- | --- | --- | --- | --- | --- |
| E01 | SQLSolver Calcite, Spark, TPC-C, TPC-H pairs | Apache-2.0 | yes | 232 + 124 + TPC-C + TPC-H pairs | covered: `sqlsolver-calcite`, `sqlsolver-spark`, `sqlsolver-tpcc`, `sqlsolver-tpch` and the bounded rows ([page](sqlsolver.md)) |
| E02 | SQL-RewriteBench | Apache-2.0 with exclusions | yes | 180 cases | covered: `sql-rewritebench` ([page](rewrite-benchmarks.md)) |
| E03 | DuckDB SQLLogicTests | MIT | yes | every `test/sql` file | covered: `engine-duckdb-slt-plain`, `engine-duckdb-slt-amplified` ([page](engine-suites.md)) |
| E04 | Original SQLite SQLLogicTest | public domain | yes (GitHub mirror) | every 4th file | covered: `engine-sqlite-slt-plain`, `engine-sqlite-slt-amplified` ([page](engine-suites.md)) |
| E05 | SQLancer | MIT | yes | generator | covered: KumoSQL's own TLP/NoREC fuzzer, `sqlancer-tlp-norec` ([page](fuzzing.md)) |
| E06 | Spider distilled test suites (`taoyds/test-suite-sql-eval`, `ruiqi-zhong/TestSuiteEval`) | Apache-2.0 (code); TestSuiteEval has no licence | code yes; test-suite databases are on Google Drive (blocked) | 557 hand-labelled ESM false negatives | **new** (batch 1, with E07): the hand-labelled equivalent pairs, downloaded at run time, refuted only on databases KumoSQL builds |
| E07 | Spider 1.0 (`taoyds/spider`) | Apache-2.0 repo, CC BY-SA 4.0 data | queries, `tables.json` yes; SQLite databases blocked (Drive, Yale site) | 1,034 dev and 7,000 train gold queries | **new** (batch 1): gold queries as rewrite inputs, checked by proof or on generated databases that respect the keys |
| E08 | Spider 2.0 | MIT | yes | 547 Lite tasks, 68 dbt | covered for the 142 published BigQuery gold queries: `spider2-bigquery` ([page](spider2-bench.md)). The SQLite and Snowflake gold are not scored (KumoSQL reads BigQuery); the dbt project archives are on Drive |
| E09 | BIRD Mini-Dev | CC BY-SA 4.0 | no: databases and gold SQL are on HuggingFace, Drive and Aliyun | 3 × 500 questions | not added: the GitHub repository holds only model predictions |
| E10 | VeriEQL | CC BY-NC-SA 4.0 | yes | 397 Calcite, 64 literature, 23,224 LeetCode | covered: six `verieql-*` rows and the bounded rows ([page](verieql.md)) |
| E11 | ParSEval (`sfu-db/ParSEval`) | Apache-2.0 | yes | 21,955 LeetCode pairs, 1,534 BIRD dev questions | not added: its LeetCode pairs are VeriEQL's LeetCode set (covered), and its BIRD pairs (gold against DAIL-SQL predictions) carry no equivalence label, so a proof could not be scored |
| E12 | QED prover tests | MIT / Apache-2.0 | yes | 373 pairs | covered: `qed-calcite` ([page](sqlsolver.md)) |
| E13 | Logos core corpus (`WindOctober/Logos`) | MIT, source families keep their terms | yes | 22 R-Bot TPC-H, 37 R-Bot DSB, 14 TPC-DS variant pairs; its VeriEQL, WeTune parts are covered | **new** (batch 1): the 73 TPC-H, DSB and TPC-DS pairs, proved and checked on generated data |
| E14 | PARROT | README says MIT, no LICENSE file | yes (1.4 GB) | 598 curated pairs not in the repository | not added: the curated pairs are leaderboard-only; the folder redistributes about 38 datasets under their own terms (see [DLBench](dlbench.md)) |
| E15 | Feedback-driven SQL optimization artifact | GPL-3.0-or-later | yes (zipped run bundles) | PostgreSQL run data | **new** (batch 2, investigate): only if the run bundles hold original and rewritten SQL with a result check; otherwise reference only |
| — | QUITE (`Yuyang-Song/QUITE`) | none stated | yes | 4,160 LLM rewrites of TPC-H, DSB, Calcite and SQLStorm queries, each flagged equal or not on the benchmark instance (587 not) | **new** (batch 1): downloaded at run time; a pair flagged unequal must never be proved |
| — | E3-Rewrite | — | — | — | not added: no public pair release was found |
| — | DBridge, EQUITAS | — | — | — | not added: DBridge optimizes imperative programs; EQUITAS has no released corpus beyond the Calcite families above |

## Complete databases (D01–D16)

| ID | Source | Licence | Download | Size | Status |
| --- | --- | --- | --- | --- | --- |
| D01 | Jaffle Shop DuckDB (`dbt-labs/jaffle_shop_duckdb`, `duckdb` branch) | Apache-2.0 | yes | 3 seeds, 5 models, YAML tests | **new** (batch 1): models rendered to SQL, seeds loaded into DuckDB, upstream dbt tests and lineage as expectations |
| D02 | Chinook | MIT-style | yes | 11 tables, no views | **new** (batch 1, with D03): `sample-databases-rewrites`, `sample-databases-pairs` ([page](sample-databases.md)) |
| D03 | Northwind (`instnwnd.sql`) | MIT | yes (raw file) | 13 tables, 16 views | **new** (batch 1): the 16 upstream views are original workload queries; `sample-databases-rewrites`, `sample-databases-pairs` ([page](sample-databases.md)) |
| D04 | Pagila | PostgreSQL | yes | 15+ tables, 11 views | **new** (batch 2): adapter for the sample-database eval |
| D05 | Sakila (`datacharmer/test_db/sakila`, the official BSD files) | New BSD | yes (the MySQL download site is blocked; the mirror holds the two official SQL files) | 16 tables, 6 views | **new** (batch 2) |
| D06 | TPC-H | Apache-2.0 generator, TPC terms | yes (`tpchgen-cli`) | 22 queries | covered: `transformation-workloads`, `sqlsolver-tpch` ([page](transformation-bench.md)) |
| D07 | TPC-DS | TPC terms | yes | 99 queries | covered: `transformation-workloads`, `analytical-sql-coverage`, `mv-benchmark` |
| D08 | Microsoft DSB | MIT | yes | 52 templates | covered: `analytical-sql-coverage` ([page](analytical-sql-coverage.md)) |
| D09 | AdventureWorks OLTP | MIT | yes (release asset) | about 70 tables, 20 views | **new** (batch 2): views as workload queries if the T-SQL adapts cleanly |
| D10 | WideWorldImporters | MIT | release holds only SQL Server `.bak` backups | — | not added: needs a SQL Server restore |
| D11 | Employees (`datacharmer/test_db`) | CC BY-SA 3.0 | yes (167 MB) | 6 tables, 3.9 M rows | **new** (batch 2): downloaded at run time, slow lane |
| D12 | Postgres Professional Airlines | MIT | no: `postgrespro.ru` is blocked | 9 tables, 133 MB dump | not added |
| D13 | Join Order Benchmark / IMDb | unverified, IMDb terms | yes | 113 queries | covered: `job-cardinality`, `job-endtoend`, `job-alternative-forms`, `mv-benchmark` |
| D14 | BigQuery Stack Overflow | hosted | needs a billed Google project | — | not added: evals run offline without credentials. The Dataform example project (below) models the same tables |
| D15 | GA4 obfuscated sample | hosted | needs a billed Google project | — | not added: as D14. GA4-shaped SQL is already in `terashim-ga4` ([page](bq-real-corpora.md)) and Spider 2.0's GA4 tasks |
| D16 | Apache Sedona SpatialBench | Apache-2.0 | yes | spatial queries | not added: KumoSQL has no GEOGRAPHY support |

## SQL before/after sources (R01–R09)

| ID | Source | Licence | Download | Size | Status |
| --- | --- | --- | --- | --- | --- |
| R01 | SQLGlot optimizer fixtures | MIT | yes | optimizer, qualify, simplify, identity | covered: `engine-sqlglot-fixtures-plain`, `-amplified`; each fixture's expected output is executed ([page](engine-suites.md)) |
| R02 | SQLSolver artifacts | Apache-2.0 | yes | as E01 | covered with E01 |
| R03 | WeTune Calcite pairs (`winoros/wetune`) | Apache-2.0 | yes | 232 pairs | covered: the same 232 pairs, in the same order, as SQLSolver's Calcite file (165 identical, 67 with small text changes). WeTune's 50 issues: `wetune-issues`. Its application workloads are in Git LFS (blocked) |
| R04 | QueryBooster experiments | GPL-3.0 | yes | 30 WeTune-application pairs, 14 rule-training pairs, 18 Twitter CAST pairs; its 228 Calcite pairs are SQLSolver's | **new** (batch 1): downloaded at run time |
| R05 | Calcite `RelOptRulesTest.xml` | Apache-2.0 | yes | — | covered: `calcite-mined` ([page](sqlsolver.md)) |
| R06 | Cosette | BSD-2-Clause | yes | 59 cases | covered: `cosette`. The two cases the source document names: `countbug` is scored (not equivalent); `SelfJoin0` is skipped because its table has no declared columns. **new** (batch 2): an adapted `SelfJoin0` with declared columns and its negative sibling without DISTINCT |
| R07 | Logos core corpus | as E13 | yes | as E13 | with E13 |
| R08 | BigQuery computation and nested-field guides | Google docs terms | `docs.cloud.google.com` is blocked here | — | left to the vendor-documentation rewrites eval of the "New equivalence evals" thread; the nested-layout migration needs a schema-mapping contract KumoSQL does not have |
| R09 | SPES | Apache-2.0 | yes | Calcite pairs | covered: `spes-only` and `bounded-spes` |
| — | R-Bot (`curtis-sun/LLM4Rewrite`) | Apache-2.0 | yes | 43 pairs | covered: `rbot-calcite`; the TPC-H and DSB pairs come with E13 |

## BigQuery and Dataform projects

| Source | Licence | Download | Size | Status |
| --- | --- | --- | --- | --- |
| Dataform BigQuery example (`dataform-co/dataform-example-project-bigquery`) | MIT | yes | 10 SQLX files | **new** (batch 1): added to the real-projects corpus |
| BigQuery Utils (`GoogleCloudPlatform/bigquery-utils`) | Apache-2.0 | yes | 136 SQL UDFs with 206 test groups; 18 views and Dataform examples | **new** (batch 1): the UDF test cases become a GoogleSQL behaviour eval (each UDF run on its test inputs, compared with the expected output); the views and Dataform examples join the real-projects corpus |
| Marketing Analytics Jumpstart Dataform | Apache-2.0 | yes | 54 SQLX files | **new** (batch 1): real-projects corpus |
| Security Analytics Dataform | Apache-2.0 (archived) | yes | 107 SQLX files | **new** (batch 1): real-projects corpus |
| ClickBench BigQuery | CC BY-NC-SA 4.0 | yes | 43 queries | covered: `clickbench-rewrites` |

## Batches

Every source marked new is one pull request with its own harness, test in `EVAL_FILES`, results file and docs page or section. Each pins its source commit, keeps original and adapted cases apart, records a baseline before any fix, keeps failures as regressions and holds out a fifth of the cases by hash. A false proof found by a new eval is kept as a regression and reported to the prover's owner rather than fixed in the eval's pull request.

- **Batch 1:** Chinook and Northwind; Jaffle Shop; the four BigQuery and Dataform projects; BigQuery Utils UDF tests; Logos TPC-H, DSB and TPC-DS pairs; QUITE; QueryBooster; Spider gold queries and the TestSuiteEval false negatives.
- **Batch 2:** Pagila, Sakila, AdventureWorks and Employees in the sample-database eval; Cosette `SelfJoin0`; the feedback-driven optimization artifact.
