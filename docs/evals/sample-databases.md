# Sample databases

Complete public sample databases, loaded whole into DuckDB from their pinned upstream scripts, with two scores: KumoSQL's rewrites on each database's workload, checked on the real data, and query pairs on its schema run through the provers. Chinook and Northwind are the first two databases and the Oracle HR and Customer Orders schemas the next two ([below](#oracle-schemas-hr-and-customer-orders), scored in their own results files); Pagila, Sakila, AdventureWorks and Employees are meant to plug in as further adapters.

| Database | Upstream (pinned) | Licence | Loaded | Declared keys |
| --- | --- | --- | --- | --- |
| Chinook 1.4.5 | [lerocha/chinook-database](https://github.com/lerocha/chinook-database) `7f67772503d7`, `ChinookDatabase/DataSources/Chinook_Sqlite.sql` | MIT-style | 11 tables, 15,607 rows | 11 primary, 11 foreign keys, 30 NOT NULL columns |
| Northwind | [microsoft/sql-server-samples](https://github.com/microsoft/sql-server-samples) `beaab06ef728`, `samples/databases/northwind-pubs/instnwnd.sql` | MIT | 13 tables, 3,308 rows | 13 primary, 13 foreign keys, 30 NOT NULL columns |

| Oracle HR | [oracle-samples/db-sample-schemas](https://github.com/oracle-samples/db-sample-schemas) `6660bad68c07`, `human_resources/hr_create.sql`, `hr_populate.sql`, `hr_code.sql` | MIT text (Copyright (c) 2023 Oracle) | 7 tables, 216 rows | 7 primary, 10 foreign keys, 17 NOT NULL columns |
| Oracle Customer Orders | the same commit, `customer_orders/co_create.sql`, `co_populate.sql` | MIT text (Copyright (c) 2023 Oracle) | 7 tables, 8,783 rows | 7 primary, 9 foreign keys, 26 NOT NULL columns |

| Score | Results file |
| --- | --- |
| 75 workload queries through every rewrite: **0 wrong in 510 executed cases, 242 rewrites verified on the real data** | `sample-databases-rewrites` |
| 54 authored pairs: **20/24 equivalent proved, 28/30 different refuted (every refutation replayed), 0 wrong** | `sample-databases-pairs` |
| Oracle HR and Customer Orders, 67 workload queries through every rewrite: **0 wrong in 458 executed cases, 206 rewrites verified on the real data** | `sample-databases-oracle-rewrites` |
| Oracle HR and Customer Orders, 106 authored pairs: **36/43 equivalent proved, 57/63 different refuted, 3 wrong** (one bounded-checker bug, see below) | `sample-databases-oracle-pairs` |

The first two rows (Chinook and Northwind) are unchanged by the Oracle work: their results files were not rewritten, the harness scores each results group on its own (`Adapter.group`, `--group`), and `tests/test_sample_db_oracle.py` pins the recorded scores of those two files.

```
python tools/sample_db_bench.py --check            # load every database and check it against upstream (seconds)
python tools/sample_db_bench.py --part pairs       # (b) only, under a minute
python tools/sample_db_bench.py --part rewrites --no-optimizer --query nw-view-invoices --show
python tools/sample_db_bench.py --write-results    # everything; about 15 minutes on 3 busy cores
python tools/sample_db_bench.py --group oracle --write-results   # only the Oracle group's two files (about 3 minutes on 2 cores)
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

Provers, in order: the structural prover (`prove_equivalent`), the algebraic/SMT prover with the declared constraints and its executed counterexample search (`prove_equivalent_algebraic(..., constraints=..., search_counterexample=True)`), then the bounded checker (at most 2 rows per table) for a counterexample only; a bounded "no difference" is not a proof. The bounded checker reads each column's declared type: a `NUMERIC(10, 2)` column holds two decimal digits and values below 1e8 (a bare `NUMERIC` keeps BigQuery's nine digits), so a counterexample fits the real column and replays in DuckDB; it first used the bare-`NUMERIC` domain, and `ch-in-to-join` produced `Total = 5.000000001`, which the real `DECIMAL(10,2)` column rounds to `5.00`. Every proof is checked on the real database. Every counterexample is completed with legal values (a fresh value for a key column, a type default for a NOT NULL column, NULL otherwise, parent rows for foreign keys), must satisfy the pair's declarations, and must separate the pair when replayed in DuckDB. `wrong` counts a proof of a pair labelled different or of a pair that differs on the real data, a replayed refutation of a pair labelled equivalent, and a counterexample that does not replay.

| Category | Pairs | Equivalent proved | Different refuted |
| --- | ---: | ---: | ---: |
| join elimination | 17 | 5/7 | 9/10 |
| aggregation | 10 | 5/5 | 4/5 |
| DISTINCT removal | 6 | 2/2 | 4/4 |
| set operation | 6 | 2/3 | 3/3 |
| NULL semantics | 5 | 2/2 | 3/3 |
| outer join | 4 | 2/2 | 2/2 |
| subquery | 4 | 2/2 | 2/2 |
| window | 2 | 0/1 | 1/1 |
| **all** | **54** | **20/24** | **28/30** |

0 wrong; all 8 siblings that drop a guarantee are refuted by a replayed database that keeps every other guarantee. Held out (13 pairs, by SHA-1 of the pair id): 7/8 proved, 5/5 refuted, 0 wrong.

Unknown (6):

- not proved: the nullable-foreign-key join read as `WHERE fk IS NOT NULL` (Chinook and Northwind; "no row-preserving mapping"), the window filter on the partition column, and the UNION ALL of two complementary filters on a NOT NULL column ("UNION shapes differ");
- not refuted: `nw-filter-vs-conditional-count` (its witness needs a category whose products are all discontinued) and `nw-self-left-join-to-reports`, where the bounded checker raises `KeyError: 'unsupported'` (`Employees` has a `BYTES` column, and the LEFT JOIN's NULL padding has no default for that type; a crash is counted as unknown). `ch-filter-vs-conditional-count` is refuted in the latest run; the executed counterexample search is time-limited and ran out in the first recorded run on a machine with a load average above 20.

## Oracle schemas (HR and Customer Orders)

`oracle-samples/db-sample-schemas` at commit `6660bad68c07bd143430ace58565b3f727e17263` (the licence file is an MIT permission notice, Copyright (c) 2023 Oracle and/or its affiliates; the inventory had MIT, a brief for this eval said UPL: the file in the repository is the authority and is committed next to the scripts). Each schema is a set of SQL\*Plus scripts: `*_create.sql` (tables, constraints, views, indexes, comments) and `*_populate.sql` (INSERTs in PL/SQL blocks). All of them are committed unchanged under `tests/fixtures/sample_databases/oracle_hr/upstream/` and `oracle_co/upstream/` with the SHA-256 of each file pinned in `tools/sample_db_bench.py` and listed in the [fixtures README](../../tests/fixtures/sample_databases/README.md); the adapted BigQuery DDL (`adapted/schema.sql`) says ADAPTED in its first line.

**Adaptation.** `NUMBER` and `NUMBER(p)` become `INT64`, `NUMBER(p, s)` becomes `NUMERIC(p, s)`, `VARCHAR2` and `CHAR` become `STRING`, `DATE` stays `DATE` (HR) and `TIMESTAMP` becomes `DATETIME` (CO keeps microseconds; the scripts have nine fractional digits, truncated). Keys are declared `NOT ENFORCED`. Dropped: CHECK constraints, UNIQUE constraints (`employees.email`, `customers.email_address`, `stores.store_name`, `order_items (product_id, order_id)`, `inventory (store_id, product_id)`: BigQuery declares only primary and foreign keys, so the provers do not know them), identity columns, sequences, indexes, `ORGANIZATION INDEX`, comments, and HR's procedures and triggers (`hr_code.sql` is pinned but holds no query). The one column that changes type is `products.product_details`: upstream stores JSON text in a `BLOB` (`UTL_RAW.CAST_TO_RAW`), here a `STRING` with the same text, because sqlglot translates BigQuery's `CAST(bytes AS STRING)` to a DuckDB cast that returns the escaped bytes and the view `product_reviews` could not otherwise run. The image and logo `BLOB` columns are NULL in every row and stay `BYTES`.

**Loading.** The harness reads the scripts as they are: `REM` and `PROMPT` lines are dropped, `ALTER TABLE t ADD (a, b)` is read as one `ADD` per constraint, a `REFERENCES parent` without a column list names the parent's primary key (HR uses it for five foreign keys), `TO_DATE`, `TO_TIMESTAMP` and `UTL_RAW.CAST_TO_RAW` calls on literals are evaluated, and one PL/SQL variable (`prod_details := '...' || chr(10) || '...'`, product 4's JSON, split over two strings for SQL\*Plus) is inlined where an INSERT uses it. The `inventory` INSERTs leave the identity column out; Oracle numbers those rows 1, 2, ... in script order and so does the loader (566 rows, ids 1 to 566). The same checks as for the first two databases pass: every file's SHA-256, the same tables, columns, NOT NULL columns, primary keys and foreign keys as the Oracle DDL (read from the Oracle script, not from the adapted DDL), each table's row count equal to the rows the script inserts, and every declared key unique and every foreign key satisfied by the real rows. Oracle publishes no row counts; the counts pinned in the adapter are the script's own (HR: 5 regions, 25 countries, 23 locations, 27 departments, 19 jobs, 107 employees, 10 job-history rows, the counts every HR installation shows; CO: 392 customers, 23 stores, 46 products, 1,950 orders, 1,892 shipments, 3,914 order items, 566 inventory rows). There is no upstream test fixture to assert against, so the loads are not checked against anything Oracle asserts about the data itself.

**Workload (a).** 67 queries: 5 `upstream-view` (the only queries the scripts hold: HR's `emp_details_view`, and CO's `customer_order_products`, `store_orders`, `product_reviews`, `product_orders`; each checked to name a view of the script) and 62 `authored` (32 HR, 30 CO: joins and outer joins, a manager self-join, a recursive management chain, aggregation and HAVING, DISTINCT on a key and on a composite key, IN, EXISTS, NOT EXISTS, NOT IN over a column with NULLs, scalar and correlated subqueries, set operations, window functions, CTE chains with an unused CTE, trivial predicates and redundant parentheses, JSON extraction, date truncation). The adaptations of the views are recorded with each query in `workload.json`: `LISTAGG(... ON OVERFLOW TRUNCATE ...) WITHIN GROUP (ORDER BY ...)` becomes `STRING_AGG(... ORDER BY ...)` (no list comes near Oracle's 4,000-byte limit), `GROUPING_ID(a, b)` becomes `GROUPING(a) * 2 + GROUPING(b)`, `JSON_TABLE(... NESTED PATH '$.reviews[*]' ...)` becomes a `LEFT JOIN UNNEST(JSON_QUERY_ARRAY(PARSE_JSON(product_details), '$.reviews'))` with `JSON_VALUE` (a nested path keeps a product without reviews, as the LEFT JOIN does: 266 rows, 261 reviews and 5 products without any), and `CREATE OR REPLACE VIEW ... WITH READ ONLY` is dropped.

| Origin | Cases | Executed | Changed and verified | No change | Error | Wrong |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| upstream views | 35 | 35 | 16 | 19 | 0 | 0 |
| authored | 423 | 423 | 190 | 233 | 0 | 0 |
| **all** | **458** | **458** | **206** | **252** | **0** | **0** |

HR 225 cases (99 changed), CO 233 (107 changed). By stage: the pipeline changed 194 of 324 cases (`format_sql` 193, `inline_single_use_ctes` 130, `remove_unused_ctes` 66, `remove_trivial_predicates` 64, `remove_redundant_parentheses` 11), `lift_subqueries` 5 of 67, the optimizer 7 of 67 (`drop_distinct` on `employee_id`, on the composite key of `job_history` and on the composite key of `order_items`, unused and single-use CTEs, a trivial predicate). Every change returned the same rows as the control. Held out (13 queries by SHA-1 of `<database>:<id>`): 0 wrong, 38 changed of 88 executed. The recursive CTE, `GROUPING SETS`, `STRING_AGG ... ORDER BY` and the JSON queries all ran and none was declined at parse time.

**Pairs (b).** 106 authored pairs (54 HR, 52 CO), 43 labelled equivalent and 63 different, in the same style as Chinook and Northwind: join elimination through a NOT NULL foreign key, a nullable foreign key read as a filter, self-joins on a primary key and on a composite key (`job_history (employee_id, start_date)`, `order_items (order_id, line_item_id)`) against the partial-key sibling, DISTINCT removal on a key, IN to EXISTS and to a join on a key, NOT IN to NOT EXISTS, COUNT of a NOT NULL column, GROUP BY of a column the key determines, outer to inner join, set operations and window filters. 26 are siblings that drop one primary key, foreign key or NOT NULL from the declarations; 3 are labelled different because Oracle's dropped UNIQUE constraints are not declarable (`SELECT DISTINCT email FROM employees` against the same without DISTINCT: equal on the real data, different under the declared keys). Every `different` label has its own witness, the real data or a small legal database, and the harness reports a label no witness supports (`labels_unverified`: 0). One label was wrong when first written and was corrected before any other change: the window pair `hr-window-filter-on-other-column` filtered on `salary` while ranking by `salary DESC`, which keeps the ranks of the rows that remain (the harness flagged it: the real data did not separate the pair); it now filters on `employee_id`. The pair is in the development split.

| Category | Pairs | Equivalent proved | Different refuted |
| --- | ---: | ---: | ---: |
| join elimination | 34 | 10/13 | 18/21 |
| aggregation | 18 | 8/8 | 9/10 |
| DISTINCT removal | 14 | 4/4 | 9/10 |
| NULL semantics | 14 | 6/6 | 8/8 |
| set operation | 10 | 2/4 | 6/6 |
| subquery | 8 | 4/4 | 4/4 |
| outer join | 4 | 2/2 | 2/2 |
| window | 4 | 0/2 | 1/2 |
| **all** | **106** | **36/43** | **57/63** |

HR 18/22 proved, 29/32 refuted; CO 18/21 proved, 28/31 refuted. Held out (12 pairs): 5/5 proved, 7/7 refuted, 0 wrong. All 26 siblings are decided away from a proof; 25 are refuted with a replayed legal database, one is unknown (below).

**The 3 wrong.** All three are one bug in a prover module, found and not fixed here: the bounded checker renders a DATE model value below ordinal 1 as `0001-01-01` (`bounded_equivalence.py`, `fromordinal(max(1, ...))`), so two `job_history` rows that differ in the DATE key column `start_date` come out equal and its counterexample repeats the primary key `(employee_id, start_date)`. The pairs are `hr-left-join-elimination-not-unique`, `hr-self-join-on-part-of-key` and `hr-distinct-on-part-of-key`; they are labelled correctly (each separates the queries on a legal witness database, or on the real data) and none is proved; the bounded checker refutes a true difference with an illegal witness, which the harness counts as wrong (a counterexample that does not replay on a legal database). Reproduction: `check_bounded("SELECT DISTINCT a, b FROM t", "SELECT a, b FROM t", schema_from_prover({"t": ["a", "d", "b"]}, {"t": TableConstraints(not_null=frozenset({"a", "d"}), keys=(("a", "d"),))}, {"t": {"a": "INT64", "d": "DATE", "b": "INT64"}}), rows=2, dialect="bigquery")` returns two identical rows `(0, 0001-01-01, 0)`. The datetime branch clamps the same way (`max(86400, ...)`). `tests/test_sample_db_oracle.py` lists the three ids as the known failures and fails if any other pair is wrong; when the checker is fixed the three become refutations and the test's list and the results file are updated.

Unknown (13): 3 of them are those wrong cases; the others are the same prover limits as for Chinook and Northwind: the nullable-foreign-key join read as `WHERE fk IS NOT NULL` (HR and CO, and HR's self-join to a filter: "no row-preserving mapping"), the window filter on the partition column (both), the UNION ALL of two complementary filters (both: "UNION shapes differ"), `co-filter-vs-conditional-count` and `co-window-filter-on-other-column` (not refuted), and `co-left-join-elimination-without-key`, where the bounded checker raises `KeyError` (CO's `stores` has `BYTES` columns, the known NULL-padding gap for that type).

**Not included.**

- **Oracle Sales History** (9 tables): its data is `sales.csv` (918,843 rows), `customers.csv` (55,500), `costs.csv` (82,112) and four smaller files, 91 MB in all. That is too large to commit and loading it at run time would make the eval depend on the network, so it is left out; the schema alone (no rows) would not show whether any rewrite is right on real data. A later pass can add it as a run-time download from the pinned commit with the same SHA-256 checks.
- **IBM FIBEN** (152 tables, an 80 MB `data.zip`): not feasible in this pass for the same reasons plus the adaptation of 152 tables.
- Oracle's `order_entry` and `product_media` schemas are not part of this eval.

**Baseline, tuning and held-out.** Nothing was tuned: the first full runs are the scores above and no rule or prover was changed. The workload and pairs were written, run once as a baseline (rewrites 0 wrong; pairs 36/43, 56/63, 3 wrong, one unverified label, the one corrected above), and rerun for the recorded scores (57/63 refuted: the executed counterexample search is time-limited and the machine was shared, so one refutation moves between runs). The held-out fifth was seen only in printed totals and per-pair verdicts and nothing was changed after seeing them. Three of the authored workload queries were rewritten before the first run because they returned no rows (an empty result cannot show a rewrite is right). DuckDB stands in for BigQuery, as above.

## Baseline, held-out cases and limits

- **Baseline.** The first full runs are the scores above; no rule or prover was changed for the first runs, so nothing was tuned on test, dev or held out. After the merge with master the bounded checker was changed to honour a declared `NUMERIC(p, s)`, found when the pair `ch-in-to-join` failed the replay; that pair is in the development split and the pairs were rerun (the rewrites row does not use the bounded checker). The only harness change outside this eval is an optional `rules` argument to `engine_suites._treat`, so the `lift_subqueries` stage reuses it.
- **Held out.** A fifth of the queries and a fifth of the pairs, by SHA-1 of `<database>:<id>`, reported apart in both results files. They were seen in the printed output of the first runs; nothing was tuned on them.
- **Constraint names.** The prover expects lower-case table and column names in `TableConstraints`; constraints keyed by `Track` instead of `track` are silently ignored (the counterexample search then returns databases that violate them). The harness lower-cases them.
- **Overlap.** No existing eval uses Chinook or Northwind. Spider's training set has a `chinook_1` database (84 questions, the same Chinook schema) and `store_1` (112 questions, a renamed Chinook); those gold queries are Spider's to score, not this eval's. Spider 2.0's dbt task `chinook001` is not scored (the dbt archives are on Google Drive).
- **Authored.** The pairs and 50 of the 75 workload queries were written for this eval; only the views, procedures and test-fixture queries come from upstream.
- **DuckDB stands in for BigQuery.** Queries are BigQuery SQL transpiled to DuckDB by sqlglot; a rewrite is checked against a control that went through the same translation.

## Adding a database

Subclass `Adapter` in `tools/sample_db_bench.py` and add it to `ADAPTERS` (an upstream that splits DDL and data across scripts or writes them in another dialect overrides `upstream_ddl`, `upstream_inserts` and `upstream_views`, as `OracleSample` does; give the class a `group` to score it in results files of its own, and an entry in `GROUPS`): the `Upstream` pins (repository, commit, path, SHA-256, licence), the upstream file that holds the DDL and INSERTs, the published row counts, `parse_datetime` or `convert` for the dialect's literals, and `renames` if a table name changes. Put the unchanged upstream files under `tests/fixtures/sample_databases/<name>/upstream/`, the adapted BigQuery DDL under `adapted/schema.sql`, and the workload and pairs next to them. Large or share-alike data (Employees: 167 MB, CC BY-SA) should be fetched at run time from the pinned commit instead of committed. `--check` then compares the adapted DDL with the upstream one and the loaded rows with upstream's counts.
