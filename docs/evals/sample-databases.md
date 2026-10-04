# Sample databases

Complete public sample databases, loaded whole into DuckDB from their pinned upstream scripts, with two scores: KumoSQL's rewrites on each database's workload, checked on the real data, and query pairs on its schema run through the provers. Chinook and Northwind are the first two databases and share one pair of results files; Sakila is the third and has results files of its own (see [Sakila](#sakila)), so adding a database never moves another's numbers. Pagila is the fourth ([its own page](sample-databases-pagila.md), with its own results files); AdventureWorks and Employees are meant to plug in as further adapters.

| Database | Upstream (pinned) | Licence | Loaded | Declared keys |
| --- | --- | --- | --- | --- |
| Chinook 1.4.5 | [lerocha/chinook-database](https://github.com/lerocha/chinook-database) `7f67772503d7`, `ChinookDatabase/DataSources/Chinook_Sqlite.sql` | MIT-style | 11 tables, 15,607 rows | 11 primary, 11 foreign keys, 30 NOT NULL columns |
| Northwind | [microsoft/sql-server-samples](https://github.com/microsoft/sql-server-samples) `beaab06ef728`, `samples/databases/northwind-pubs/instnwnd.sql` | MIT | 13 tables, 3,308 rows | 13 primary, 13 foreign keys, 30 NOT NULL columns |
| Sakila (Spatial 0.9) | [datacharmer/test_db](https://github.com/datacharmer/test_db) `e324b56193ca`, `sakila/sakila-mv-schema.sql` and `sakila/sakila-mv-data.sql` | New BSD (Oracle) | 16 tables, 47,273 rows | 16 primary, 22 foreign keys, 73 NOT NULL columns |

| Score | Results file |
| --- | --- |
| 75 workload queries through every rewrite: **0 wrong in 510 executed cases, 242 rewrites verified on the real data** | `sample-databases-rewrites` |
| 54 authored pairs: **20/24 equivalent proved, 28/30 different refuted (every refutation replayed), 0 wrong** | `sample-databases-pairs` |
| Sakila: 57 workload queries through every rewrite: **0 wrong in 394 executed cases, 180 rewrites verified on the real data** | `sample-databases-sakila-rewrites` |
| Sakila: 64 authored pairs: **23/27 equivalent proved, 37/37 different refuted (every refutation replayed), 0 wrong** | `sample-databases-sakila-pairs` |

```
python tools/sample_db_bench.py --check            # load both databases and check them against upstream (seconds)
python tools/sample_db_bench.py --part pairs       # (b) only, under a minute
python tools/sample_db_bench.py --part rewrites --no-optimizer --query nw-view-invoices --show
python tools/sample_db_bench.py --write-results    # everything; about 15 minutes on 3 busy cores
python tools/sample_db_bench.py --database sakila --write-results   # only Sakila's two results files (about 6 minutes)
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

## Baseline, held-out cases and limits

- **Baseline.** The first full runs are the scores above; no rule or prover was changed for the first runs, so nothing was tuned on test, dev or held out. After the merge with master the bounded checker was changed to honour a declared `NUMERIC(p, s)`, found when the pair `ch-in-to-join` failed the replay; that pair is in the development split and the pairs were rerun (the rewrites row does not use the bounded checker). The only harness change outside this eval is an optional `rules` argument to `engine_suites._treat`, so the `lift_subqueries` stage reuses it.
- **Held out.** A fifth of the queries and a fifth of the pairs, by SHA-1 of `<database>:<id>`, reported apart in both results files. They were seen in the printed output of the first runs; nothing was tuned on them.
- **Constraint names.** The prover expects lower-case table and column names in `TableConstraints`; constraints keyed by `Track` instead of `track` are silently ignored (the counterexample search then returns databases that violate them). The harness lower-cases them.
- **Overlap.** No existing eval uses Chinook or Northwind. Spider's training set has a `chinook_1` database (84 questions, the same Chinook schema) and `store_1` (112 questions, a renamed Chinook); those gold queries are Spider's to score, not this eval's. Spider 2.0's dbt task `chinook001` is not scored (the dbt archives are on Google Drive).
- **Authored.** For Chinook and Northwind the pairs and 50 of the 75 workload queries were written for this eval; only the views, procedures and test-fixture queries come from upstream. For Sakila the 64 pairs and 40 of the 57 workload queries are authored.
- **DuckDB stands in for BigQuery.** Queries are BigQuery SQL transpiled to DuckDB by sqlglot; a rewrite is checked against a control that went through the same translation.

## Adding a database

Subclass `Adapter` in `tools/sample_db_bench.py` and add it to `ADAPTERS`: the `Upstream` pins (repository, commit, path, SHA-256, licence), the upstream file that holds the DDL and INSERTs, the published row counts, `parse_datetime` or `convert` for the dialect's literals, and `renames` if a table name changes. Put the unchanged upstream files under `tests/fixtures/sample_databases/<name>/upstream/`, the adapted BigQuery DDL under `adapted/schema.sql`, and the workload and pairs next to them. Large or share-alike data (Employees: 167 MB, CC BY-SA) should be fetched at run time from the pinned commit instead of committed. `--check` then compares the adapted DDL with the upstream one and the loaded rows with upstream's counts. Sakila needed four small hooks on `Adapter`, each with a default that leaves the other databases alone: `upstream_text` (the DDL and the INSERTs are two files, read together), `upstream_views` (a MySQL script ends a view with `;`, not `GO`), `trigger_rows` (rows an upstream trigger adds while loading) and `results_order` (a database outside Chinook and Northwind writes `sample-databases-<name>-rewrites` and `-pairs`, so its numbers and theirs never share a file). A database whose DDL and data are separate files sets `ddl_file`; one whose data is not `INSERT`s overrides `upstream_rows` (Pagila reads `COPY` blocks); one with partitioned tables overrides `upstream_tables`. Give a later database its own pair of results files with `results_group`, `results_order` and `docs_page`, so adding it leaves the Chinook and Northwind rows alone; `python tools/sample_db_bench.py --database <name> --write-results` then writes just that database's files.
