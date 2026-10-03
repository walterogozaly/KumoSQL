# Sample databases

Complete public sample databases, loaded whole into DuckDB from their pinned upstream scripts, with two scores: KumoSQL's rewrites on each database's workload, checked on the real data, and query pairs on its schema run through the provers. Chinook and Northwind are the first two databases; Pagila, Sakila, AdventureWorks and Employees are meant to plug in as further adapters.

| Database | Upstream (pinned) | Licence | Loaded | Declared keys |
| --- | --- | --- | --- | --- |
| Chinook 1.4.5 | [lerocha/chinook-database](https://github.com/lerocha/chinook-database) `7f67772503d7`, `ChinookDatabase/DataSources/Chinook_Sqlite.sql` | MIT-style | 11 tables, 15,607 rows | 11 primary, 11 foreign keys, 30 NOT NULL columns |
| Northwind | [microsoft/sql-server-samples](https://github.com/microsoft/sql-server-samples) `beaab06ef728`, `samples/databases/northwind-pubs/instnwnd.sql` | MIT | 13 tables, 3,308 rows | 13 primary, 13 foreign keys, 30 NOT NULL columns |

| Score | Results file |
| --- | --- |
| 75 workload queries through every rewrite: **0 wrong in 510 executed cases, 242 rewrites verified on the real data** | `sample-databases-rewrites` |
| 54 authored pairs: **20/24 equivalent proved, 27/30 different refuted (every refutation replayed), 0 wrong** | `sample-databases-pairs` |

```
python tools/sample_db_bench.py --check            # load both databases and check them against upstream (seconds)
python tools/sample_db_bench.py --part pairs       # (b) only, under a minute
python tools/sample_db_bench.py --part rewrites --no-optimizer --query nw-view-invoices --show
python tools/sample_db_bench.py --write-results    # everything; about 15 minutes on 3 busy cores
```

## Data and adaptation

The upstream files are committed unchanged with their licences in [`tests/fixtures/sample_databases`](../../tests/fixtures/sample_databases/README.md) (both are under 1.1 MB), next to the adapted files, which say so in their first line:

- `adapted/schema.sql` is BigQuery DDL: upstream types become BigQuery types (T-SQL `money` becomes `NUMERIC(19, 4)`, `real` becomes `FLOAT64`, `bit` an `INT64` holding 0 or 1, `image` becomes `BYTES`), keys are declared `NOT ENFORCED`, and CHECK constraints, defaults, identity columns and indexes are dropped. Northwind's `nchar` values are loaded without their padding.
- The rows are never copied: the harness reads every `INSERT` of the upstream script (single- and multi-row `VALUES`, `N'...'` strings, `0x` binary literals, Northwind's `m/d/yyyy` dates) and converts each value to the adapted column type.

Every run checks the load against upstream, and the eval stops on any difference:

- the SHA-256 of each upstream file;
- the same tables, columns (in order), NOT NULL columns, primary keys and foreign keys as the upstream DDL (read from the upstream script, including Northwind's later `ALTER TABLE ... ADD CONSTRAINT`);
- each table's row count equal to the rows the upstream script inserts and to the counts upstream publishes: Chinook's own test fixture (`ChinookSqliteFixture.cs`: 25 genres, 3,503 tracks, 2,240 invoice lines, ...) and the documented Northwind counts (830 orders, 2,155 order lines, ...);
- every declared key unique, every NOT NULL column without NULLs and every foreign key satisfied by the real rows;
- the other assertions of Chinook's test fixture (the last row of each table, every invoice total equal to the sum of its lines, no invoice without lines).

## Workload (a): every rewrite, checked on the real data

| Origin | Queries | What it is |
| --- | ---: | --- |
| `upstream-view` | 16 | Northwind's 16 `CREATE VIEW` bodies, adapted to BigQuery SQL. The harness checks each one names a view of the upstream script. The views are also created in DuckDB, because four of them read other views, as upstream does |
| `upstream-procedure` | 7 | the `SELECT` of each Northwind stored procedure, with its parameters bound (for example `@CustomerID = 'ALFKI'`) |
| `upstream-test` | 2 | the two non-trivial queries of Chinook's test fixture; the result the fixture asserts is checked too |
| `authored` | 50 | 26 Chinook and 24 Northwind queries written for this eval: inner, outer and self joins with NULLs, aggregation and HAVING, DISTINCT (also on a key), IN, EXISTS, NOT EXISTS, NOT IN with NULLs, scalar and correlated subqueries, UNION ALL, UNION, INTERSECT and EXCEPT DISTINCT, window functions (RANK, DENSE_RANK, LAG, LEAD, running sums), CTE chains with an unused CTE, trivial predicates and redundant parentheses |

Each adaptation is recorded with the query in `workload.json` (`adaptation`): `UNION` becomes `UNION DISTINCT`; the strings `'19970101'` and `'19971231'` that SQL Server converts become `DATETIME` literals; `FirstName + ' ' + LastName` becomes `CONCAT`; `CONVERT(money, x)` becomes `ROUND(x, 4)`; `CONVERT(int, x)` becomes `CAST(TRUNC(x) AS INT64)`; `DATENAME(yy, d)` and `CONVERT(nvarchar(22), d, 111)` become `FORMAT_DATETIME`; `SET ROWCOUNT 10` becomes `LIMIT 10`; `alias = expr` becomes `expr AS alias`; quoted names become backticks. Views 15 and 16 (`Summary of Sales by Quarter` and `... by Year`) have the same body upstream and are both kept.

Each query is checked with the engine-suites machinery (`tools/engine_suites.py`, see [engine suites](engine-suites.md)): the query runs in DuckDB as the control; then

1. **pipeline**: KumoSQL's canonical rule order on the query and on four variants that give the rules work (wrapped in a subquery, wrapped in a CTE, behind an unused CTE, with `1 = 1 AND TRUE` added);
2. **lift_subqueries**, the one rule outside the canonical order;
3. **optimizer**: the proof-gated `query_optimizer.optimize` with the declared primary keys and NOT NULL columns (it can drop a DISTINCT or GROUP BY that a key makes redundant).

A rewrite is wrong when its result multiset differs from the control (rerun with DuckDB's optimizer off before it counts) or when it no longer runs.

| Origin | Cases | Executed | Changed and verified | No change | Error | Wrong |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| upstream views | 111 | 107 | 59 | 48 | 4 | 0 |
| upstream procedures | 49 | 48 | 21 | 27 | 1 | 0 |
| upstream tests | 14 | 14 | 7 | 7 | 0 | 0 |
| authored | 341 | 341 | 155 | 186 | 0 | 0 |
| **all** | **515** | **510** | **242** | **268** | **5** | **0** |

By stage: the pipeline changed 231 of 365 cases (`format_sql` 226, `inline_single_use_ctes` 147, `remove_unused_ctes` 74, `remove_trivial_predicates` 73, `remove_redundant_parentheses` 39), `lift_subqueries` 4 of 75, the optimizer 7 of 75 (DISTINCT dropped on a key twice, a GROUP BY dropped inside `NOT IN`, unused CTEs, single-use CTEs inlined, trivial predicates). Every change returned the same rows as the control. Held out (12 queries, by SHA-1 of the query id): 0 wrong, 37 changed of 82 executed.

The 5 errors are `lift_subqueries` on Northwind's nested parenthesised joins (`FROM a JOIN (b JOIN c ON ...) ON ...`, in 4 views and 1 procedure): the rule lifts the parenthesised join as if it were a subquery and writes `WITH __lifted_subquery_001 AS (b INNER JOIN c ON ...)`, which does not parse. The rule notices (`output_parse_error`) and returns the query unchanged, so nothing wrong is emitted; it is a missed rewrite, and the smallest case is `SELECT a.x FROM a INNER JOIN (b INNER JOIN c ON b.y = c.y) ON a.y = b.y`.

## Pairs (b): the provers on the declared keys

54 authored pairs (29 Chinook, 25 Northwind), each labelled `equivalent` or `different` under the declared keys, in `pairs.json` with the reason. 24 equivalent pairs are rewrites that hold on these schemas: join elimination through a NOT NULL foreign key to a key, a nullable foreign key join read as `IS NOT NULL`, LEFT JOIN elimination on a key, DISTINCT removal on a (composite) key, IN to EXISTS and to a join on a key, NOT IN to NOT EXISTS on NOT NULL columns, COUNT of a NOT NULL column, a dependent GROUP BY column, HAVING on the group key, filter pushdown through GROUP BY and window partitions, outer to inner join under a null-rejecting filter, INTERSECT to IN on a NOT NULL column. 30 are negative siblings: the same rewrite where it does not hold (a nullable foreign key, a non-unique join column, NOT IN over NULLs, UNION ALL against UNION, a filter against a conditional count, INTERSECT's NULL matching), 8 of them the equivalent pair itself with one guarantee removed from the declarations (`drop`: a foreign key, a primary key or a NOT NULL).

Every `different` label has its own evidence, independent of the provers: the pair returns different rows on the real data (`witness: "real"`, 15 pairs), or a small witness database separates it (15 pairs), completed with legal values and checked against the declarations that remain. An `equivalent` pair that differs on the real data would be reported as a label error; none does.

Provers, in order: the structural prover (`prove_equivalent`), the algebraic/SMT prover with the declared constraints and its executed counterexample search (`prove_equivalent_algebraic(..., constraints=..., search_counterexample=True)`), then the bounded checker (at most 2 rows per table) for a counterexample only; a bounded "no difference" is not a proof. Every proof is checked on the real database. Every counterexample is completed with legal values (a fresh value for a key column, a type default for a NOT NULL column, NULL otherwise, parent rows for foreign keys), must satisfy the pair's declarations, and must separate the pair when replayed in DuckDB. `wrong` counts a proof of a pair labelled different or of a pair that differs on the real data, a replayed refutation of a pair labelled equivalent, and a counterexample that does not replay.

| Category | Pairs | Equivalent proved | Different refuted |
| --- | ---: | ---: | ---: |
| join elimination | 17 | 5/7 | 9/10 |
| aggregation | 10 | 5/5 | 3/5 |
| DISTINCT removal | 6 | 2/2 | 4/4 |
| set operation | 6 | 2/3 | 3/3 |
| NULL semantics | 5 | 2/2 | 3/3 |
| outer join | 4 | 2/2 | 2/2 |
| subquery | 4 | 2/2 | 2/2 |
| window | 2 | 0/1 | 1/1 |
| **all** | **54** | **20/24** | **27/30** |

0 wrong; all 8 siblings that drop a guarantee are refuted by a replayed database that keeps every other guarantee. Held out (13 pairs, by SHA-1 of the pair id): 7/8 proved, 5/5 refuted, 0 wrong.

Unknown (7):

- not proved: the nullable-foreign-key join read as `WHERE fk IS NOT NULL` (Chinook and Northwind; "no row-preserving mapping"), the window filter on the partition column, and the UNION ALL of two complementary filters on a NOT NULL column ("UNION shapes differ");
- not refuted: `nw-filter-vs-conditional-count` (its witness needs a category whose products are all discontinued), `nw-self-left-join-to-reports`, where the bounded checker raises `KeyError: 'unsupported'` (`Employees` has a `BYTES` column, and the LEFT JOIN's NULL padding has no default for that type; a crash is counted as unknown), and `ch-filter-vs-conditional-count`. That last pair is refuted on an idle machine (three reruns), but the executed counterexample search is time-limited and ran out in the recorded run on a machine with a load average above 20.

## Baseline, held-out cases and limits

- **Baseline.** The first full runs are the scores above; no rule or prover was changed for this eval, so there is no tuning on test, dev or held out. The only harness change outside this eval is an optional `rules` argument to `engine_suites._treat`, so the `lift_subqueries` stage reuses it.
- **Held out.** A fifth of the queries and a fifth of the pairs, by SHA-1 of `<database>:<id>`, reported apart in both results files. They were seen in the printed output of the first runs; nothing was tuned on them.
- **Constraint names.** The prover expects lower-case table and column names in `TableConstraints`; constraints keyed by `Track` instead of `track` are silently ignored (the counterexample search then returns databases that violate them). The harness lower-cases them.
- **Overlap.** No existing eval uses Chinook or Northwind. Spider's training set has a `chinook_1` database (84 questions, the same Chinook schema) and `store_1` (112 questions, a renamed Chinook); those gold queries are Spider's to score, not this eval's. Spider 2.0's dbt task `chinook001` is not scored (the dbt archives are on Google Drive).
- **Authored.** The pairs and 50 of the 75 workload queries were written for this eval; only the views, procedures and test-fixture queries come from upstream.
- **DuckDB stands in for BigQuery.** Queries are BigQuery SQL transpiled to DuckDB by sqlglot; a rewrite is checked against a control that went through the same translation.

## Adding a database

Subclass `Adapter` in `tools/sample_db_bench.py` and add it to `ADAPTERS`: the `Upstream` pins (repository, commit, path, SHA-256, licence), the upstream file that holds the DDL and INSERTs, the published row counts, `parse_datetime` or `convert` for the dialect's literals, and `renames` if a table name changes. Put the unchanged upstream files under `tests/fixtures/sample_databases/<name>/upstream/`, the adapted BigQuery DDL under `adapted/schema.sql`, and the workload and pairs next to them. Large or share-alike data (Employees: 167 MB, CC BY-SA) should be fetched at run time from the pinned commit instead of committed. `--check` then compares the adapted DDL with the upstream one and the loaded rows with upstream's counts.
