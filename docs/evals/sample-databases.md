# Sample databases

Complete public sample databases, loaded whole into DuckDB from their pinned upstream scripts, with two scores: KumoSQL's rewrites on each database's workload, checked on the real data, and query pairs on its schema run through the provers. Chinook and Northwind are the first two databases and share one pair of results files; Sakila is the third and has results files of its own (see [Sakila](#sakila)), so adding a database never moves another's numbers. Pagila is the fourth ([its own page](sample-databases-pagila.md), with its own results files) and the Oracle HR and Customer Orders schemas the fifth and sixth ([below](#oracle-schemas-hr-and-customer-orders), one pair of results files each); Oracle Sales History is a further Oracle schema whose 91 MB of rows are downloaded at run time ([below](#oracle-sales-history-run-time-download), its own results files); AdventureWorks and Employees are meant to plug in as further adapters.

| Database | Upstream (pinned) | Licence | Loaded | Declared keys |
| --- | --- | --- | --- | --- |
| Chinook 1.4.5 | [lerocha/chinook-database](https://github.com/lerocha/chinook-database) `7f67772503d7`, `ChinookDatabase/DataSources/Chinook_Sqlite.sql` | MIT-style | 11 tables, 15,607 rows | 11 primary, 11 foreign keys, 30 NOT NULL columns |
| Northwind | [microsoft/sql-server-samples](https://github.com/microsoft/sql-server-samples) `beaab06ef728`, `samples/databases/northwind-pubs/instnwnd.sql` | MIT | 13 tables, 3,308 rows | 13 primary, 13 foreign keys, 30 NOT NULL columns |
| Sakila (Spatial 0.9) | [datacharmer/test_db](https://github.com/datacharmer/test_db) `e324b56193ca`, `sakila/sakila-mv-schema.sql` and `sakila/sakila-mv-data.sql` | New BSD (Oracle) | 16 tables, 47,273 rows | 16 primary, 22 foreign keys, 73 NOT NULL columns |
| Oracle HR | [oracle-samples/db-sample-schemas](https://github.com/oracle-samples/db-sample-schemas) `6660bad68c07`, `human_resources/hr_create.sql`, `hr_populate.sql`, `hr_code.sql` | MIT text (Copyright (c) 2023 Oracle) | 7 tables, 216 rows | 7 primary, 10 foreign keys, 17 NOT NULL columns |
| Oracle Customer Orders | the same commit, `customer_orders/co_create.sql`, `co_populate.sql` | MIT text (Copyright (c) 2023 Oracle) | 7 tables, 8,783 rows | 7 primary, 9 foreign keys, 26 NOT NULL columns |
| Oracle Sales History | the same commit, `sales_history/sh_create.sql`, `sh_populate.sql`, `sh_install.sql`, and six CSV files fetched at run time | MIT text (Copyright (c) 2023 Oracle) | 9 tables, 1,063,396 rows | 7 primary, 10 foreign keys, 110 NOT NULL columns |

| Score | Results file |
| --- | --- |
| 75 workload queries through every rewrite: **0 wrong in 510 executed cases, 242 rewrites verified on the real data** | `sample-databases-rewrites` |
| 54 authored pairs: **20/24 equivalent proved, 28/30 different refuted (every refutation replayed), 0 wrong** | `sample-databases-pairs` |
| Sakila: 57 workload queries through every rewrite: **0 wrong in 394 executed cases, 180 rewrites verified on the real data** | `sample-databases-sakila-rewrites` |
| Sakila: 64 authored pairs: **23/27 equivalent proved, 37/37 different refuted (every refutation replayed), 0 wrong** | `sample-databases-sakila-pairs` |
| Oracle HR: 33 workload queries through every rewrite: **0 wrong in 225 executed cases, 99 rewrites verified on the real data** | `sample-databases-oracle_hr-rewrites` |
| Oracle HR: 54 authored pairs: **18/22 equivalent proved, 32/32 different refuted (every refutation replayed), 0 wrong** | `sample-databases-oracle_hr-pairs` |
| Oracle Customer Orders: 34 workload queries through every rewrite: **0 wrong in 233 executed cases, 107 rewrites verified on the real data** | `sample-databases-oracle_co-rewrites` |
| Oracle Customer Orders: 52 authored pairs: **18/21 equivalent proved, 29/31 different refuted (every refutation replayed), 0 wrong** | `sample-databases-oracle_co-pairs` |
| Oracle Sales History: 45 workload queries through every rewrite: **0 wrong in 310 executed cases, 145 rewrites verified on the real data** | `sample-databases-oracle_sh-rewrites` |
| Oracle Sales History: 82 authored pairs: **28/32 equivalent proved, 48/50 different refuted (every refutation replayed), 0 wrong** | `sample-databases-oracle_sh-pairs` |

```
python tools/sample_db_bench.py --check            # load every database and check it against upstream (seconds)
python tools/sample_db_bench.py --part pairs       # (b) only, under a minute
python tools/sample_db_bench.py --part rewrites --no-optimizer --query nw-view-invoices --show
python tools/sample_db_bench.py --write-results    # everything; about 15 minutes on 3 busy cores
python tools/sample_db_bench.py --database sakila --write-results   # only Sakila's two results files (about 6 minutes)
python tools/sample_db_bench.py --database oracle_hr --write-results   # only Oracle HR's two results files
python tools/sample_db_bench.py --database oracle_sh --write-results   # downloads 91 MB once; about 25 minutes on 2 busy cores
python tools/sample_db_oracle_sh.py                                    # only fetch and verify Oracle SH's CSV files
```

`--write-results` writes the combined Chinook and Northwind files from those two databases' cases only, and one pair of files per further database from that database's cases only; naming only some of the combined databases is refused.

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

## Sakila

Sakila (a DVD rental store: 16 tables with composite keys, two foreign keys from one table to another, nullable foreign keys, a cycle of foreign keys between `store` and `staff`, 7 views and 6 stored routines) is the third database. It has its own results files, `sample-databases-sakila-rewrites` and `sample-databases-sakila-pairs`, so the Chinook and Northwind rows stay as recorded.

### Data and adaptation

The upstream files are Oracle's Sakila Spatial 0.9 scripts as the [`datacharmer/test_db`](https://github.com/datacharmer/test_db/tree/master/sakila) mirror holds them (`sakila-mv-schema.sql`, 23 KB, and `sakila-mv-data.sql`, 3.4 MB, both New BSD, Copyright (c) 2014 Oracle Corporation; the licence is the header of each file), pinned at commit `e324b56193ca506ab7cc1ab143a9153d8c4535d7`:

| File | SHA-256 |
| --- | --- |
| `sakila/sakila-mv-schema.sql` | `61c30abd47a0126e9e901a911b8a115e1920f0910ce1b11ff3abc764ad65df53` |
| `sakila/sakila-mv-data.sql` | `cf9328c055ed43c6862332438670fdad68c6236d051fa090c6bcb56cf5895bc2` |
| `sakila/README.md` (the mirror's note on its two changes) | `05fe520851c87f662d5cbd8a50655d361e08b2ad56bedf495ed4d3b3bc1fb3e0` |

They are not the two files of the MySQL download (`sakila-schema.sql`, `sakila-data.sql`, version 1.2), which sit on a site this repository's runners cannot reach; the mirror's files are Sakila Spatial, which Giuseppe Maxia made loadable by any MySQL 5.x in 2015 (the FULLTEXT index and the GEOMETRY column are guarded by version comments). The tables, keys and data are Sakila's; `address.location` is the spatial column that the original 1.2 files lack.

The files are committed unchanged under `tests/fixtures/sample_databases/sakila/upstream/`, next to `adapted/schema.sql`, which says it is adapted in its first line:

- MySQL types become BigQuery types: every integer type, also `UNSIGNED`, and `YEAR` become `INT64`; `BOOLEAN` (`TINYINT(1)`) becomes `INT64` holding 0 or 1, as Northwind's `bit`; `VARCHAR`, `CHAR`, `TEXT` and `ENUM`/`SET` (loaded as their text) become `STRING`; `DECIMAL(p, s)` becomes `NUMERIC(p, s)`; `DATETIME` and `TIMESTAMP` become `DATETIME`; `BLOB` and `GEOMETRY` (MySQL's internal geometry bytes) become `BYTES`.
- Keys are declared `NOT ENFORCED`. `DEFAULT`, `AUTO_INCREMENT`, `ON UPDATE`, the foreign keys' `ON DELETE`/`ON UPDATE` actions, indexes, FULLTEXT and SPATIAL keys, the engine and the character set are dropped. The two UNIQUE keys (`store.manager_staff_id`; `rental`'s `rental_date`, `inventory_id`, `customer_id`) are not declared to the provers (the harness reads primary keys, foreign keys and NOT NULL), so no pair relies on them.
- The rows are never copied: the harness reads every `INSERT` of the data script. The script runs on MySQL 5.7.5 and later, which execute `/*!50705 ... */` comments, so `address.location` (a `0x...` literal) is read as live text. `film_text` has no `INSERT`: the schema script's trigger `ins_film` fills it from `film`, and the harness does the same (a trigger hook on the adapter, one row per film). The other triggers (`upd_film`, `del_film`, and the three that set a date on insert, which the data script creates after the rows are inserted) do not run while loading.

Every run checks the load against upstream, as for the other databases: both SHA-256 pins; the same tables, columns (in order), NOT NULL columns, primary keys and foreign keys as the upstream DDL (read from the schema script, with its trigger, procedure and function bodies set aside); every table's row count equal to the rows the data script inserts (plus the trigger's) and to the counts usually published for Sakila (1,000 films, 16,044 rentals, 16,049 payments, 5,462 film-actor rows, 4,581 inventory items, ...; they are written into the adapter from memory of those listings, the MySQL site being unreachable here, and the pinned data meets them); every primary key unique, every NOT NULL column without NULLs and every foreign key satisfied by the real rows; and that `film_text` equals `film` on id, title and description.

### Workload

| Origin | Queries | What it is |
| --- | ---: | --- |
| `upstream-view` | 7 | the `CREATE VIEW` bodies of the schema script (`customer_list`, `film_list`, `nicer_but_slower_film_list`, `staff_list`, `sales_by_store`, `sales_by_film_category`, `actor_info`), adapted to BigQuery SQL; the harness checks each names a view of the script |
| `upstream-procedure` | 10 | the SELECTs of `rewards_report` (2), `film_in_stock`, `film_not_in_stock`, `get_customer_balance` (3), `inventory_held_by_customer` and `inventory_in_stock` (2), with their parameters bound (for example `p_customer_id = 130`: the three SELECTs of `get_customer_balance` add up to a balance of 0) |
| `authored` | 40 | written for this eval: joins, outer joins (LEFT, RIGHT, FULL), self joins, aggregation and HAVING, DISTINCT (also on a key), IN, EXISTS, NOT EXISTS, NOT IN with NULLs, scalar and correlated subqueries, set operations, window functions, a CTE chain with an unused CTE, trivial predicates, joins to tables whose columns are not read |

Sakila ships no test queries. Each adaptation is recorded with its query (`adaptation`): `GROUP_CONCAT(x SEPARATOR ', ')` becomes `STRING_AGG(x, ', ' ORDER BY ...)` (MySQL leaves the order unspecified; the ORDER BY makes the result deterministic, which a comparison of rewrites needs); the columns MySQL accepts in a grouped query because they depend on the group key are added to `GROUP BY`; `IF(cu.active, ...)` becomes `IF(cu.active = 1, ...)`; the `_utf8` introducers and `sakila.` prefixes are dropped; `TO_DAYS(a) - TO_DAYS(b)` becomes `DATE_DIFF(DATE(a), DATE(b), DAY)`; the stored function `inventory_in_stock` is inlined as `NOT EXISTS` of a rental with no `return_date`, which is what its body computes; the temporary table of `rewards_report` is inlined as a derived table; `actor_info`'s `GROUP_CONCAT(DISTINCT ... ORDER BY c.name)` becomes `STRING_AGG(DISTINCT x, '; ' ORDER BY x)` over a derived table, because BigQuery and DuckDB need the ORDER BY of a DISTINCT aggregate to be the aggregated expression. The adapted views return the figures usually quoted for Sakila (`sales_by_store`: 33,679.79 and 33,726.77; `film_list`: 997 rows). They are not run on BigQuery or MySQL.

The scoring is the one of [workload (a)](#workload-a-every-rewrite-checked-on-the-real-data): pipeline (plain and four variants), `lift_subqueries`, the proof-gated optimizer with the declared keys.

| Origin | Cases | Executed | Changed and verified | No change | Error | Wrong |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| upstream views | 49 | 49 | 22 | 27 | 0 | 0 |
| upstream procedures | 70 | 70 | 31 | 39 | 0 | 0 |
| authored | 275 | 275 | 127 | 148 | 0 | 0 |
| **all** | **394** | **394** | **180** | **214** | **0** | **0** |

By stage: the pipeline changed 169 of 280 cases (`format_sql` 169, `inline_single_use_ctes` 113, `remove_trivial_predicates` 60, `remove_unused_ctes` 57, `remove_redundant_parentheses` 15), `lift_subqueries` 4 of 57 (`actor_info`, the `rewards_report` join, a derived table, the category-revenue join), the optimizer 7 of 57: DISTINCT dropped on a key twice (`film_id` of `film`; the `customer_id` list of an `IN`), a GROUP BY column dropped because the store's key fixes it (`sk-a29`), the always-true and NOT NULL predicates, the unused and single-use CTEs. Every change returned the same rows as the control. Held out (11 queries, by SHA-1 of the query id): 0 wrong, 32 changed of 75 executed. No query was changed or dropped after a run. Unlike Northwind, no query has a nested parenthesised join, so `lift_subqueries` has no error here.

### Pairs

64 authored pairs (27 equivalent, 37 different; 15 of the different ones are siblings that drop one primary key, foreign key or NOT NULL), in `pairs.json` with the reason. They use what is particular to Sakila: composite keys (`film_actor`, `film_category`), a nullable foreign key that is NULL in 5 real rows (`payment.rental_id`), a nullable foreign key that is NULL in every row (`film.original_language_id`) next to `film.language_id`, which is NOT NULL, a NOT NULL foreign key chain (`address` to `city` to `country`) and the cycle between `store` and `staff`. 17 `different` labels are shown by the real data and 20 by a small witness database; no `equivalent` pair differs on the real data.

| Category | Pairs | Equivalent proved | Different refuted |
| --- | ---: | ---: | ---: |
| join elimination | 20 | 7/8 | 12/12 |
| aggregation | 12 | 5/5 | 7/7 |
| DISTINCT removal | 8 | 3/3 | 5/5 |
| NULL semantics | 7 | 3/3 | 4/4 |
| set operation | 7 | 2/3 | 4/4 |
| outer join | 4 | 1/2 | 2/2 |
| subquery | 4 | 2/2 | 2/2 |
| window | 2 | 0/1 | 1/1 |
| **all** | **64** | **23/27** | **37/37** |

0 wrong; all 15 siblings that drop a guarantee are refuted by a replayed database that keeps every other guarantee. Of the 37 refutations, 11 come from the algebraic counterexample search and 26 from the bounded checker. Held out (14 pairs, by SHA-1 of the pair id): 5/5 proved, 9/9 refuted, 0 wrong.

Unknown (4), the same gaps as Chinook and Northwind: the nullable-foreign-key join read as `WHERE fk IS NOT NULL` and `LEFT JOIN ... WHERE` against the inner join with the condition in `ON` ("no row-preserving mapping"), the window filter on the partition column ("no row-preserving mapping") and the UNION ALL of two complementary filters ("UNION shapes differ").

### Baseline and what it showed

The first run was the baseline; no rule or prover was changed, and no query or pair was tuned. Its pairs: 23/27 proved, 14/37 refuted and 23 counterexamples counted wrong because they did not replay. Both causes are in the harness and the counterexamples, not in a proof:

- 22 came from the bounded checker, which has no `BYTES` domain and reports NULL for `address.location`, a NOT NULL `BYTES` column, in every database it returns (it also fills every table, not only the ones the pair reads). The harness read that as a violated NOT NULL and refused the counterexample, as it should. The fix is in the harness's completion: a NULL the bounded checker reports for a `BYTES` column is no value, so the completion chooses one (an empty value for a NOT NULL column). After it 37/37 are refuted, each by a database that satisfies every declaration. The Chinook and Northwind pairs are unchanged by it (20 proved and 29 refuted on `master` and on this branch; `master` itself refutes one more than the recorded 28 on an idle machine, see the timing note above).
- 1 is `store` joined to `staff` on `staff.store_id`: the algebraic prover correctly answers that the queries differ (the pair's own witness separates them), but its counterexample is a store row with no staff row, which violates the foreign key `store.manager_staff_id`. Because the foreign keys of `store` and `staff` form a cycle, every legal completion the harness tries (type defaults for the missing columns, parent rows for the foreign keys) gives a staff row that makes both queries return the store. The pair is kept in `pairs.json` under `not_scored` with this reason and is not in the 64.

The baseline printed every pair, held-out ones included, so the harness change is recorded as seen on test: it was generic (any `BYTES` column) and made no pair-specific change, but it is not a clean held-out result. The workload run needed no change.

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

**Pairs (b).** 106 authored pairs (54 HR, 52 CO; HR 22 equivalent and 32 different, CO 21 and 31), 43 labelled equivalent and 63 different, in the same style as Chinook and Northwind: join elimination through a NOT NULL foreign key, a nullable foreign key read as a filter, self-joins on a primary key and on a composite key (`job_history (employee_id, start_date)`, `order_items (order_id, line_item_id)`) against the partial-key sibling, DISTINCT removal on a key, IN to EXISTS and to a join on a key, NOT IN to NOT EXISTS, COUNT of a NOT NULL column, GROUP BY of a column the key determines, outer to inner join, set operations and window filters. 26 are siblings that drop one primary key, foreign key or NOT NULL from the declarations; 3 are labelled different because Oracle's dropped UNIQUE constraints are not declarable (`SELECT DISTINCT email FROM employees` against the same without DISTINCT: equal on the real data, different under the declared keys). Every `different` label has its own witness, the real data or a small legal database, and the harness reports a label no witness supports (`labels_unverified`: 0). One label was wrong when first written and was corrected before any other change: the window pair `hr-window-filter-on-other-column` filtered on `salary` while ranking by `salary DESC`, which keeps the ranks of the rows that remain (the harness flagged it: the real data did not separate the pair); it now filters on `employee_id`. The pair is in the development split.

| Category | Pairs | Equivalent proved | Different refuted |
| --- | ---: | ---: | ---: |
| join elimination | 34 | 10/13 | 20/21 |
| aggregation | 18 | 8/8 | 9/10 |
| DISTINCT removal | 14 | 4/4 | 10/10 |
| NULL semantics | 14 | 6/6 | 8/8 |
| set operation | 10 | 2/4 | 6/6 |
| subquery | 8 | 4/4 | 4/4 |
| outer join | 4 | 2/2 | 2/2 |
| window | 4 | 0/2 | 2/2 |
| **all** | **106** | **36/43** | **61/63** |

The scores are per database: HR 18/22 proved, 32/32 refuted; CO 18/21 proved, 29/31 refuted (`sample-databases-oracle_hr-pairs`, `sample-databases-oracle_co-pairs`). 0 wrong. Of the 61 refutations 14 come from the algebraic counterexample search and 47 from the bounded checker; every one was replayed on a legal database that satisfies the declared constraints. Held out (12 pairs): 5/5 proved, 7/7 refuted, 0 wrong. All 26 siblings are decided away from a proof; 25 are refuted with a replayed legal database, one is unknown (below).

**The DATE key counterexamples.** In the baseline run three HR pairs were counted wrong (`hr-left-join-elimination-not-unique`, `hr-self-join-on-part-of-key`, `hr-distinct-on-part-of-key`). They were one bug in the bounded checker, not a false proof: it rendered a DATE (and DATETIME) model value below its first day as `0001-01-01`, so two `job_history` rows that differ in the DATE key column `start_date` came out equal and the counterexample repeated the primary key `(employee_id, start_date)`, which the replay gate refuses. The bug was fixed on master by a separate change (the clamp in `bounded_equivalence`); this eval changed no prover. After merging that fix and rerunning the pairs unchanged, the three are refuted by counterexamples that replay on a legal database (HR 32/32 refuted) and the recorded Oracle pairs have 0 wrong, with no list of known failures. Nothing else moved in HR; in CO `co-window-filter-on-other-column`, unknown in the baseline, is now refuted too (29/31 instead of 28/31); I did not isolate whether that is the same fix or the time limit of the search.

Unknown (9), the same prover limits as for Chinook and Northwind: the nullable-foreign-key join read as `WHERE fk IS NOT NULL` (HR and CO, and HR's self-join to a filter: "no row-preserving mapping"), the window filter on the partition column (both), the UNION ALL of two complementary filters (both: "UNION shapes differ"), `co-filter-vs-conditional-count` (not refuted), and `co-left-join-elimination-without-key`, where the bounded checker raises `KeyError` (CO's `stores` has `BYTES` columns, the known NULL-padding gap for that type).

## Oracle Sales History (run-time download)

The same repository and commit as HR and Customer Orders (`oracle-samples/db-sample-schemas` at `6660bad68c07bd143430ace58565b3f727e17263`, the MIT permission notice committed next to the scripts). Sales History (SH) is a star schema: dimensions `times` (a DATE primary key, 1,826 days), `products` (72), `customers` (55,500, each with a `countries` row), `channels` (5) and `promotions` (503), the facts `sales` (918,843 rows) and `costs` (82,112), and `supplementary_demographics` (4,500, one per customer). It has no primary key on either fact table upstream (the script's own comment says a sale is identified by the combination of its foreign keys, which is not a constraint) and none is invented, so a pair that needs a fact-table key is labelled different.

**What is committed and what is not.** The four small files (`sh_create.sql`, `sh_populate.sql`, `sh_install.sql`, `README.md`) and the licence are committed unchanged under `tests/fixtures/sample_databases/oracle_sh/upstream/` with their SHA-256 pinned in the adapter and listed in the [fixtures README](../../tests/fixtures/sample_databases/README.md). `sh_populate.sql` INSERTs only channels, countries and products; the other six tables are `LOAD <table> <file>.csv` lines for SQLcl. Those six files (91 MB, `sales.csv` alone 74 MB) are not committed. `tools/sample_db_oracle_sh.py` downloads each from `raw.githubusercontent.com` at the pinned commit into a cache (`$KUMOSQL_BENCH_DATA/oracle-sh`, default `~/.cache/kumosql-bench/oracle-sh`; the file is written whole and renamed so parallel runs never read half a file) and checks its SHA-256 on every use: a file that does not match is an error (`ValueError`), a file that cannot be fetched is `OSError`, which the tests turn into a skip. `python tools/sample_db_bench.py --check` fetches and verifies them too. The table to file mapping is read from the pinned script's own `LOAD` lines, and the harness refuses a script whose `LOAD` lines name different files than the pinned six.

**Adaptation.** As for HR and CO (`NUMBER` to `INT64` or `NUMERIC(p, s)`, `VARCHAR2` and `CHAR` to `STRING`, `DATE` stays `DATE`; keys declared `NOT ENFORCED`); dropped: range partitioning of `sales` and `costs`, `COMPRESS`, bitmap and text indexes, `CREATE DIMENSION`, the materialized views (their defining queries are workload queries), comments, statistics and `WITH READ ONLY`. Every SH `NUMBER` column without a scale holds whole numbers in the data, and the load checks it. The adapted DDL (`adapted/schema.sql`) says ADAPTED in its first line.

**Loading.** The three INSERTed tables are read as for HR and CO; `TO_DATE(..., 'yyyy-mm-dd-hh24-mi-ss')` literals with a zero time of day are evaluated (a time of day in a DATE column raises), and Oracle's empty string is NULL. The CSV files are read by DuckDB's `read_csv` with every column as text and cast by the harness: an empty field is NULL (as SQLcl's `LOAD` reads it), the blanks `sales.csv` pads its last field with are trimmed, and a value is refused unless it is exactly representable in its declared type (an integer in an `INT64` column, at most the declared scale in a `NUMERIC`, `YYYY-MM-DD` in a date), so a cast never rounds or reinterprets a value silently. The row counts are compared with the `provided` column of the installation verification at the end of the pinned `sh_install.sql` (5 channels, 35 countries, 72 products, 55,500 customers, 503 promotions, 1,826 days, 918,843 sales, 82,112 costs, 4,500 supplementary demographics), and the CSV records are counted with Python's `csv` reader, not with DuckDB, so the loader does not check itself. The same checks as for the other databases pass: every file's SHA-256, the tables, columns, NOT NULL columns, primary keys and foreign keys of the Oracle DDL (read from the Oracle script, not from the adapted DDL), and every declared key unique and every foreign key satisfied by the real rows. Loading takes about 15 seconds for each worker that needs the data.

**Workload (a).** 45 queries: 3 `upstream-view` (the only queries the scripts hold: the view `profits`, a comma join of `costs` and `sales` on four keys, and the materialized views `cal_month_sales_mv` and `fweek_pscat_sales_mv`; each checked to name a view of the script, adapted as recorded with it in `workload.json`) and 42 `authored` on the star schema: dimension to fact joins and a snowflake chain (sales to customers to countries), aggregation by month, category, channel and fiscal year with HAVING, DISTINCT on the primary key of `products` and `times`, IN, EXISTS, NOT EXISTS, NOT IN over a nullable and a NOT NULL column, scalar and correlated subqueries, set operations, rank, running total and `LAG` windows, CTE chains with an unused CTE, trivial predicates and redundant parentheses, a left join to `supplementary_demographics`, pre-aggregation through the day dimension, conditional aggregation, and queries over the `profits` view. A query may run 60 seconds on the real data (`Adapter.query_timeout_s`; the engine suites' 5 seconds is too short for the full join of the two fact tables in `profits`).

| Origin | Cases | Executed | Changed and verified | No change | Error | Wrong |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| upstream views | 21 | 21 | 9 | 12 | 0 | 0 |
| authored | 289 | 289 | 136 | 153 | 0 | 0 |
| **all** | **310** | **310** | **145** | **165** | **0** | **0** |

By stage: the pipeline changed 133 of 220 cases (`format_sql` 133, `inline_single_use_ctes` 89, `remove_unused_ctes` 45, `remove_trivial_predicates` 44, `remove_redundant_parentheses` 9), `lift_subqueries` 6 of 45, the optimizer 6 of 45 (`drop_distinct` on the primary key of `products` and of `times`, `drop_join` of the left join to `supplementary_demographics` on its primary key, an unused and a single-use CTE, a trivial predicate, `merge_projection`). Every change returned the same rows as the control. Held out (11 queries by SHA-1 of `oracle_sh:<id>`): 0 wrong, 33 changed of 75 executed.

**Pairs (b).** 82 authored pairs, 32 equivalent and 50 different, in the same style as the other databases and built for the star schema: aggregate pushdown through the dimension joins (summing per day first and joining the day to its month, the same through `products` and through `customers` and `countries`; `COUNT`, `MAX`; against the siblings that average the averages, count distinct customers per day, or lose the fact filter), join elimination through a NOT NULL foreign key from a fact to a dimension (product, time, the snowflake chain, `costs` to `products`), the dimension-key uniqueness every one of them depends on (a sibling without the dimension's primary key, one without the foreign key, one with the foreign key's column nullable), left joins on a primary key, self-joins on a primary key and on a non-key column, DISTINCT removal on the key of `products` and of `times`, `COUNT` of a NOT NULL against a nullable column, `COALESCE` and `IS NOT NULL` on NOT NULL and nullable columns, IN to EXISTS and to a join on a key and on a fact, NOT IN to NOT EXISTS, outer to inner join, set operations and window filters. 28 are siblings that drop one primary key, foreign key or NOT NULL from the declarations (`drop` in `pairs.json`). Every `different` label has its own witness, the real data (17 pairs) or a small legal database; the harness reports a label no witness supports (`labels_unverified`: 0). Most fact-table pairs filter `sales` to channel 9 (2,074 rows) or to one day so that the real-data check stays small.

| Category | Pairs | Equivalent proved | Different refuted |
| --- | ---: | ---: | ---: |
| join elimination | 26 | 8/8 | 18/18 |
| aggregation | 24 | 10/11 | 11/13 |
| DISTINCT removal | 10 | 2/3 | 7/7 |
| NULL semantics | 7 | 3/3 | 4/4 |
| set operation | 7 | 2/3 | 4/4 |
| subquery | 4 | 2/2 | 2/2 |
| outer join | 2 | 1/1 | 1/1 |
| window | 2 | 0/1 | 1/1 |
| **all** | **82** | **28/32** | **48/50** |

The scores are those of the two results files, `sample-databases-oracle_sh-rewrites` and `sample-databases-oracle_sh-pairs`: 28/32 equivalent proved, 48/50 different refuted, 0 wrong. Of the 48 refutations 12 come from the algebraic counterexample search and 36 from the bounded checker; every one was replayed on a legal database that satisfies the declared constraints (`counterexample_ok`). Held out (17 pairs): 2/4 proved, 12/13 refuted, 0 wrong. All 28 siblings are refuted with a replayed legal database. Unknown (6): the same limits as for the other databases: DISTINCT over a join to a dimension (`sh-distinct-through-key-join`), a GROUP BY that adds a column the key determines (`sh-group-by-day-and-its-month`), the UNION ALL of two complementary filters ("UNION shapes differ"), the window filter on the partition column, and two pairs labelled different (the average of daily averages, and a filter against a conditional count) for which no counterexample was found.

**The DATE key.** `times.time_id` is a DATE primary key, so the pairs that drop it (`sh-fk-join-elimination-time-without-key`, `sh-distinct-on-time-key-without-key`) need two dimension rows that differ in a DATE and a counterexample that does not repeat a key. This is where the bounded checker once rendered a DATE below its first day as `0001-01-01` (see the HR section above): on current master it does not, and no SH pair is counted wrong or marked a prover bug (`prover_bugs`: 0). The replay gate would catch it if it came back: a counterexample that repeats a declared key is a prover bug, never a refutation, and never a wrong count. The test keeps the DATE-keyed pairs as must-be-refuted-and-replayed.

**Baseline, tuning and held-out.** The baseline is the first run, before anything was changed: rewrites 0 wrong in 303 executed of 304 cases (the one unsupported case was the unrewritten `profits` view, a full join of the two fact tables, interrupted at the engine suites' 5-second query limit) and pairs 28/32 proved, 48/50 refuted, 0 wrong, with two `different` labels no witness supported (`sh-pushdown-average-of-averages` and `sh-pushdown-count-distinct-of-daily-counts`, both in the development split: the real data of channel 9 did not separate them). After the baseline three things changed and nothing else: the two pairs got a small legal database as their witness (their labels are unchanged), the query limit for this database was raised to 60 seconds (`Adapter.query_timeout_s`, so the `profits` case runs and is compared like the others: 310 of 310 executed, 0 wrong), and nothing in a rewrite rule or a prover was touched. The baseline output listed every pair's verdict, held-out ones included, and no change was made in response to any of them; the held-out fifth was never used to choose a rewrite, a pair or a threshold. Besides the 91 MB download, the shared harness got additive hooks only (`Upstream.remote`, `Adapter.remote`, `pin_path`, `available`, `inserted_counts`, `query_timeout_s`); the shared sweeps over every database (the load check and the pair floors of `tests/test_sample_db_bench.py`) leave a database with run-time data to its own test file, `tests/test_sample_db_oracle_sh.py`, which skips when GitHub cannot be reached. All 82 pairs and 42 of the 45 workload queries are authored; the three upstream queries are the view and the two materialized views the script ships.

**Not included.**

- **Oracle Sales History** is [its own section](#oracle-sales-history-run-time-download).
- **IBM FIBEN** (152 tables, an 80 MB `data.zip`): not feasible in this pass for the same reasons plus the adaptation of 152 tables.
- Oracle's `order_entry` and `product_media` schemas are not part of this eval.

**Baseline, tuning and held-out.** Nothing was tuned. The workload and pairs were written and run once as a baseline (rewrites 0 wrong; pairs 36/43 proved, 56/63 refuted, 3 wrong from the bounded checker's DATE bug above, one unverified label, the one corrected above); the recorded scores are the rerun after master's fix for that bug, with the same workload, pairs, harness and provers (rewrites 0 wrong again; pairs 36/43, 61/63, 0 wrong). Besides the three pairs the fix repaired, one CO pair moved from unknown to refuted; the executed counterexample search is time-limited, so a refutation can also move between runs on a shared machine. The held-out fifth was seen only in printed totals and per-pair verdicts and nothing was changed after seeing them. Three of the authored workload queries were rewritten before the first run because they returned no rows (an empty result cannot show a rewrite is right). DuckDB stands in for BigQuery, as above.

## Baseline, held-out cases and limits

- **Baseline.** The first full runs are the scores above; no rule or prover was changed for the first runs, so nothing was tuned on test, dev or held out. After the merge with master the bounded checker was changed to honour a declared `NUMERIC(p, s)`, found when the pair `ch-in-to-join` failed the replay; that pair is in the development split and the pairs were rerun (the rewrites row does not use the bounded checker). The only harness change outside this eval is an optional `rules` argument to `engine_suites._treat`, so the `lift_subqueries` stage reuses it.
- **Held out.** A fifth of the queries and a fifth of the pairs, by SHA-1 of `<database>:<id>`, reported apart in both results files. They were seen in the printed output of the first runs; nothing was tuned on them.
- **Constraint names.** The prover expects lower-case table and column names in `TableConstraints`; constraints keyed by `Track` instead of `track` are silently ignored (the counterexample search then returns databases that violate them). The harness lower-cases them.
- **Overlap.** No existing eval uses Chinook or Northwind. Spider's training set has a `chinook_1` database (84 questions, the same Chinook schema) and `store_1` (112 questions, a renamed Chinook); those gold queries are Spider's to score, not this eval's. Spider 2.0's dbt task `chinook001` is not scored (the dbt archives are on Google Drive).
- **Authored.** For Chinook and Northwind the pairs and 50 of the 75 workload queries were written for this eval; only the views, procedures and test-fixture queries come from upstream. For Sakila the 64 pairs and 40 of the 57 workload queries are authored.
- **DuckDB stands in for BigQuery.** Queries are BigQuery SQL transpiled to DuckDB by sqlglot; a rewrite is checked against a control that went through the same translation.

## Adding a database

Subclass `Adapter` in `tools/sample_db_bench.py` and add it to `ADAPTERS`: the `Upstream` pins (repository, commit, path, SHA-256, licence), the upstream file that holds the DDL and INSERTs, the published row counts, `parse_datetime` or `convert` for the dialect's literals, and `renames` if a table name changes. Put the unchanged upstream files under `tests/fixtures/sample_databases/<name>/upstream/`, the adapted BigQuery DDL under `adapted/schema.sql`, and the workload and pairs next to them. Large or share-alike data (Employees: 167 MB, CC BY-SA) should be fetched at run time from the pinned commit instead of committed. A database with run-time data (Oracle Sales History) marks its pins `Upstream(..., remote=True)` and its adapter `remote = True`, overrides `pin_path` (where a pinned file is), `available` (False when it cannot be fetched), `inserted_counts` and `connect`, and keeps its tests in a file of its own; the shared sweeps skip a `remote` adapter. `--check` then compares the adapted DDL with the upstream one and the loaded rows with upstream's counts. Sakila needed four small hooks on `Adapter`, each with a default that leaves the other databases alone: `upstream_text` (the DDL and the INSERTs are two files, read together), `upstream_views` (a MySQL script ends a view with `;`, not `GO`), `trigger_rows` (rows an upstream trigger adds while loading) and `results_order` (a database outside Chinook and Northwind writes `sample-databases-<name>-rewrites` and `-pairs`, so its numbers and theirs never share a file). A database whose DDL and data are separate files sets `ddl_file`; one whose data is not `INSERT`s overrides `upstream_rows` (Pagila reads `COPY` blocks); one with partitioned tables overrides `upstream_tables`. Give a later database its own pair of results files with `results_group`, `results_order` and `docs_page`, so adding it leaves the Chinook and Northwind rows alone; `python tools/sample_db_bench.py --database <name> --write-results` then writes just that database's files. The Oracle adapters override `ddl_text`, `upstream_tables`, `upstream_rows` and `upstream_views` (SQL\*Plus scripts) and add one hook of their own, `omitted(table, column, index)`, the value of a column an INSERT leaves out (the identity column of `inventory`).
