# Sample databases: AdventureWorks

AdventureWorks OLTP, Microsoft's bicycle-manufacturer sample, loaded whole into DuckDB from its pinned release, with the same two scores as the other [sample databases](sample-databases.md): KumoSQL's rewrites on its workload, checked on the real data, and query pairs on its schema run through the provers. It is the first adapter whose data is **downloaded at run time** instead of committed (the data files are 91 MB). Chinook, Northwind, Sakila, Pagila and Oracle keep their own results files and numbers; AdventureWorks has its own.

| Database | Upstream (pinned) | Licence | Loaded | Declared keys |
| --- | --- | --- | --- | --- |
| AdventureWorks OLTP (release `adventureworks`, 2022 data) | [microsoft/sql-server-samples](https://github.com/microsoft/sql-server-samples) release asset `AdventureWorks-oltp-install-script.zip` (SHA-256 `58962e94ea38`): the script `instawdb.sql` (SHA-256 `fd1be672069c`) and 69 data files, each pinned in `members.sha256` | MIT, Copyright (c) Microsoft Corporation | 70 tables, 759,240 rows | 70 primary, 90 foreign keys, 404 NOT NULL columns |

| Score | Results file |
| --- | --- |
| 71 workload queries through every rewrite: **0 wrong in 488 executed cases, 214 rewrites verified on the real data** | `sample-databases-adventureworks-rewrites` |
| 67 authored pairs: **24/28 equivalent proved, 32/39 different refuted (every refutation replayed), 0 wrong** | `sample-databases-adventureworks-pairs` |

```
python tools/sample_db_bench.py --check --database adventureworks      # download (once), load and check against upstream (about 30 seconds)
python tools/sample_db_bench.py --database adventureworks --part pairs  # (b) only
python tools/sample_db_bench.py --database adventureworks --write-results   # both parts and both results files
```

`adventureworks` is in `DOWNLOADED`, not in `ADAPTERS`: a default run and the tests that loop over every database never touch the network, and its own tests skip when the release cannot be fetched. Name it with `--database`.

## Data and adaptation

The release asset is one zip. The harness downloads it once into `$KUMOSQL_BENCH_DATA` (default `~/.cache/kumosql-bench`), refuses it unless its SHA-256 is the pinned one, and checks every member against `members.sha256` (`AdventureWorks.check_members`). The install script (330 KB) and the repository's licence are committed unchanged under `tests/fixtures/sample_databases/adventureworks/upstream/`, so the schema is checked against upstream without the network. The 69 data files are never committed.

Nothing is copied by hand. The adapter reads the script: the DDL (`CREATE TABLE`, then the keys that `ALTER TABLE ... ADD` declares after the load, one constraint at a time), the `BULK INSERT` statements (which file feeds which table, with which field and row terminators: a tab and a line feed, or `+|` and `&|` followed by a line feed) and the `CREATE VIEW` bodies. A full load parses 760,000 rows, which takes minutes, so the loaded database is built once into a DuckDB file whose name holds a hash of the zip, the adapted DDL and the reader code, and copied from there.

`adapted/schema.sql` is BigQuery DDL and says so in its first line. What changes:

- Types: `int`, `smallint` and `tinyint` become `INT64`, `bit` and the alias types `Flag` and `NameStyle` `BOOL`, `money` `NUMERIC(19, 4)`, `smallmoney` `NUMERIC(10, 4)`, `decimal(p, s)` `NUMERIC(p, s)`, `datetime` `DATETIME`, `date` `DATE`, `nvarchar`, `nchar`, `varchar` and the alias types `Name`, `Phone`, `AccountNumber` and `OrderNumber` `STRING`, `varbinary(max)` `BYTES`.
- Types with no BigQuery counterpart keep the text the data files write, in a `STRING`: `uniqueidentifier` (a GUID), `xml`, `time` (`Shift.StartTime`) and the CLR types `hierarchyid` and `geography`, which the files write as hex (`Employee.OrganizationNode`, `Document.DocumentNode`, `Address.SpatialLocation`). `nchar` values keep their padding (`Product.ProductLine` is `'R '`, `Document.Revision` `'0    '`).
- Computed columns are ordinary columns holding the values the data files give: `Customer.AccountNumber`, `SalesOrderHeader.SalesOrderNumber` and `TotalDue`, `SalesOrderDetail.LineTotal`, the purchase order and work order totals, and the two hierarchyid levels. Those upstream declares `ISNULL(expression, constant)` are NOT NULL, as SQL Server infers them; the two levels are nullable.
- Schema names are dropped (no two tables share a name). `DatabaseLog`, a heap the script's DDL trigger fills with the statements the script itself runs, is left out; `ErrorLog` stays and is empty.
- Primary and foreign keys are `NOT ENFORCED`. Dropped: `IDENTITY`, defaults, `CHECK` and `UNIQUE` constraints, `ROWGUIDCOL`, every index (BigQuery has no `UNIQUE`, so the alternate keys, such as `Person.rowguid` and `Product.ProductNumber`, are not declared keys), the XML schema collections, full-text catalogs, triggers, functions, procedures and extended properties.

Every run checks the load against upstream, and the eval stops on any difference:

- the SHA-256 of the committed script and licence, of the zip and of each member of the zip;
- the same tables, columns (in order), NOT NULL columns, primary keys and foreign keys as upstream's DDL, and that every table the script loads has exactly the rows of its data file (upstream publishes no row counts);
- every declared key unique, every NOT NULL column without NULLs and every declared foreign key satisfied by the real rows;
- 8 assertions that the script's computed-column formulas hold on the loaded rows (`TotalDue = SubTotal + TaxAmt + Freight` for sales and purchase orders, `LineTotal` of both detail tables, `StockedQty` of purchase details and work orders, `SalesOrderNumber`, `AccountNumber` and the level of a NULL `OrganizationNode`).

### What the pinned release does

- **The dates are the 2022 release's.** Orders run from 2022-05-30 to 2025-06-29, purchase orders from 2022-04-15 to 2025-09-21, but `BillOfMaterials` dates are all in 2021 and `ProductReview` in 2024. The bill-of-materials queries therefore use a check date in 2021.
- **`vSalesPersonSalesByFiscalYears` is all NULL on this data.** The view pivots the fiscal years 2002 to 2004, which no order falls in. The query is adapted as upstream has it, and an authored query pivots 2023 to 2025.
- **Individual customers have no store.** `Customer.StoreID` is NULL for 18,484 of 19,820 customers, `SalesOrderHeader.SalesPersonID` for 27,659 of 31,465 orders: good traps for joins on nullable foreign keys.
- **`SubTotal` is not the exact sum of `LineTotal`.** The two differ in 695 orders (four and six decimal places), so the pairs do not rely on it.
- **`ProductAssemblyID` is NULL for the 25 raw materials** of `BillOfMaterials`, so `NOT IN` over it returns nothing.
- Four `ProductReview` comments hold line feeds inside a line-terminated file; the reader joins the continuation lines.

## Workload (a): every rewrite, checked on the real data

| Origin | Queries | What it is |
| --- | ---: | --- |
| `upstream-view` | 12 | the 12 portable `CREATE VIEW` bodies of `instawdb.sql` (`vEmployee`, `vEmployeeDepartment`, `vEmployeeDepartmentHistory`, `vIndividualCustomer`, `vProductAndDescription`, `vSalesPerson`, `vSalesPersonSalesByFiscalYears`, `vStateProvinceCountryRegion`, `vStoreWithAddresses`, `vStoreWithContacts`, `vVendorWithAddresses`, `vVendorWithContacts`). The harness checks each names a view of the script |
| `upstream-procedure` | 11 | the `SELECT`s of the upstream functions and procedures with their parameters bound: the four branches of `ufnGetContactInformation` and their `UNION ALL`, `ufnGetProductDealerPrice`, `ufnGetProductListPrice`, `ufnGetProductStandardCost`, `ufnGetStock`, and the recursive queries of `uspGetBillOfMaterials` and `uspGetWhereUsedProductID` |
| `authored` | 48 | written for this eval: joins on NOT NULL, nullable and composite foreign keys, `LEFT JOIN` elimination and anti joins, `DISTINCT` on single and composite keys, `IN`, `EXISTS`, `NOT EXISTS` and `NOT IN` with NULLs, scalar and correlated subqueries, set operations, window functions, CTEs with an unused one, trivial predicates, `DATE` and `DATETIME` functions, string functions and `LIKE`, and a pivot of the fiscal years this release has |

Each adaptation from T-SQL is recorded with the query in `workload.json` (`adaptation`): square brackets and schema prefixes dropped; `+` string concatenation becomes `||`; `DATEADD(m, 6, d)` becomes `DATETIME_ADD(d, INTERVAL 6 MONTH)`; `ISNULL` becomes `COALESCE`; the alias `at` (`AddressType`) becomes `aty`; the column `Group` is quoted with backticks; `PIVOT (SUM(SubTotal) FOR FiscalYear IN (...))` becomes one `SUM(IF(FiscalYear = year, SubTotal, NULL))` per year grouped by the other columns (the same rows); a CTE with a column list becomes `WITH RECURSIVE` with the names given in the anchor member, and `OPTION (MAXRECURSION 25)` is dropped; the `IF EXISTS` guards and the `INSERT` into the function's table variable are dropped (an empty `SELECT` inserts nothing).

**What was declined, and why** (listed in `workload.json` under `declined`, and checked: every upstream view is either a workload query or declined):

- 8 of the 20 views read an `xml` column with XQuery (`value()`, `nodes()`, namespace declarations): `vAdditionalContactInfo`, `vPersonDemographics`, `vJobCandidate`, `vJobCandidateEmployment`, `vJobCandidateEducation`, `vProductModelCatalogDescription`, `vProductModelInstructions` and `vStoreWithDemographics`. BigQuery has no counterpart, and inventing a regular-expression stand-in would not be upstream's query.
- `uspGetEmployeeManagers` and `uspGetManagerEmployees` recurse over `hierarchyid` methods (`GetAncestor`, `ToString`) of `Employee.OrganizationNode`, which the data files write as hex.
- `uspSearchCandidateResumes` uses full-text `CONTAINS` and `FREETEXT`; the update procedures, the triggers and the error-logging procedures change data; the scalar functions (`ufnGetAccountingStartDate`, `ufnGetAccountingEndDate`, the three status-text functions, `ufnLeadingZeros`) contain no table, so there is no query.

Each query goes through the same stages as the other sample databases (see [the first page](sample-databases.md#workload-a-every-rewrite-checked-on-the-real-data)): the canonical rule pipeline on the query and four variants, `lift_subqueries`, and the proof-gated optimizer with the declared keys. A rewrite is wrong when its result multiset differs from the unrewritten control (rerun with DuckDB's optimizer off before it counts) or when it no longer runs.

| Origin | Cases | Executed | Changed and verified | No change | Unsupported | Wrong |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| upstream views | 84 | 84 | 37 | 47 | 0 | 0 |
| upstream procedures | 74 | 74 | 26 | 48 | 0 | 0 |
| authored | 330 | 330 | 151 | 179 | 0 | 0 |
| **all** | **488** | **488** | **214** | **274** | **0** | **0** |

By stage: the pipeline changed 200 of 346 cases, `lift_subqueries` 5 of 71 (the derived tables of the fiscal-year view and of `aw-a21`, `aw-a22`, `aw-a25` and `aw-a42`), the optimizer 9 of 71. The optimizer's changes are the ones the declared keys license: a `LEFT JOIN` to `ProductModel` on its key (`aw-a05`), a `DISTINCT` on `BusinessEntityAddress`'s composite key and after a key join (`aw-a07`, `aw-a09`), an unused CTE and an inlined single-use one (`aw-a23`, `aw-a24`), trivial and redundant predicates (`aw-a25`, `aw-a39`, `aw-a44`) and a `LEFT JOIN` with `DISTINCT` (`aw-a48`). Every change returned the same rows as the control. Held out (11 queries, by SHA-1 of `adventureworks:<query id>`): 0 wrong, 33 changed of 75 executed. No query errored or timed out, the two recursive queries included.

The optimizer does not remove the joins of `aw-a01` to `aw-a03`, `aw-a36` and `aw-a37` (a `NOT NULL` foreign key onto a primary key): the harness gives it keys and NOT NULL columns but not foreign keys, as for every sample database. The pairs below show the provers can prove that rewrite when they are given the foreign keys.

## Pairs (b): the provers on the declared keys

67 authored pairs, each labelled `equivalent` or `different` under the declared keys, in `pairs.json` with the reason. 28 equivalent pairs are rewrites that hold on this schema: join elimination through a NOT NULL foreign key (one and two hops, to a single, a composite and a one-to-one key), a nullable foreign key join read as `IS NOT NULL`, `LEFT JOIN` elimination on a key, a self join on a key, `DISTINCT` removal on a single, composite and four-column key (one of its columns a `DATE`), `COUNT` of a NOT NULL column, `COUNT(DISTINCT key)`, a dependent `GROUP BY` column, `HAVING` on the group key, filter pushdown through `GROUP BY` and a window partition, `IN` to `EXISTS` and to a join on a key, `NOT IN` to `NOT EXISTS` on a NOT NULL column, outer to inner join under a null-rejecting filter, `INTERSECT` to `IN`, `IS NULL`, `COALESCE` and `NOT (a = b)` on NOT NULL columns. 39 are negative siblings: the same rewrite where it does not hold, 13 of them the equivalent pair itself with one guarantee removed from the declarations (`drop`: a foreign key, a primary key or a NOT NULL).

The key-dependent siblings that are particular to this schema: `Product.Name` and `Product.ProductNumber` are unique upstream (unique indexes) but BigQuery cannot declare that, so a self join on the name and `SELECT DISTINCT ProductNumber` are not provably removable although the data agrees; `SalesOrderDetailID` alone is not `SalesOrderDetail`'s key, only the pair with `SalesOrderID` is, so `DISTINCT` and `COUNT(DISTINCT ...)` over it stay (the column is unique in the data); `Customer.StoreID` is nullable, so the store join is not removable but is a filter; joining `SalesOrderDetail` to `SpecialOfferProduct` on the offer alone, not on the composite foreign key, repeats lines; `BillOfMaterials.ProductAssemblyID` is nullable and NULL for raw materials, which makes `NOT IN` over it differ from `NOT EXISTS`.

Every `different` label has its own evidence, independent of the provers: the pair returns different rows on the real data (`witness: "real"`, 19 pairs), or a small witness database separates it (20 pairs), completed with legal values and checked against the declarations that remain. An `equivalent` pair that differs on the real data would be reported as a label error; none does. Provers, in order: the structural prover, the algebraic/SMT prover with the declared constraints and its executed counterexample search, then the bounded checker (at most 2 rows per table) for a counterexample only. Every proof is checked on the real database. Every counterexample is completed with legal values, must satisfy the pair's declarations, and must separate the pair when replayed in DuckDB. All 24 proofs came from the algebraic prover; the 32 refutations are 8 algebraic and 24 bounded counterexamples.

| Category | Pairs | Equivalent proved | Different refuted |
| --- | ---: | ---: | ---: |
| join elimination | 21 | 7/8 | 10/13 |
| aggregation | 13 | 5/6 | 7/7 |
| DISTINCT removal | 11 | 4/4 | 4/7 |
| NULL semantics | 9 | 4/4 | 5/5 |
| set operation | 5 | 1/2 | 3/3 |
| subquery | 4 | 2/2 | 1/2 |
| outer join | 2 | 1/1 | 1/1 |
| window | 2 | 0/1 | 1/1 |
| **all** | **67** | **24/28** | **32/39** |

0 wrong; 11 of the 13 siblings that drop a guarantee are refuted by a replayed database that keeps every other guarantee. Held out (11 pairs, by SHA-1 of `adventureworks:<pair id>`): 5/5 proved, 4/6 refuted, 0 wrong.

Unknown (11), nothing wrong is claimed for any of them:

- not proved (4): the nullable foreign key join read as `WHERE fk IS NOT NULL` and the window filter on the partition column ("no row-preserving mapping"), the `LEFT JOIN` count against the correlated `COUNT(*)` subquery (not supported by the algebraic prover) and the `UNION ALL` of complementary filters ("UNION shapes differ"). All four are true equivalences the prover cannot show.
- not refuted (7): `DISTINCT` against a plain select on a non-key and on part of the composite key ("only one side removes duplicate rows"), the same after a key join that fans out ("no row-preserving mapping"), the two left join pairs ("UNION shapes differ"), the composite-key join on one column and the `IN` join without a key ("no row-preserving mapping"). The labels are right (each has its own witness, replayed in DuckDB); the provers just did not find the counterexample themselves.

## Bugs found

None new. The known bounded-checker DATE/DATETIME clamp ([issue #494](https://github.com/walterogozaly/KumoSQL/issues/494)) did not appear: the pair with a `DATE` in its key (`EmployeeDepartmentHistory.StartDate`) was refuted with a legal counterexample. The BYTES problem did not appear either (no pair reads a `BYTES` column). No prover module was changed.

## Baseline, held-out cases and limits

- **Baseline.** The first full runs, before any change: rewrites as above (they took 18 minutes on a shared machine) and pairs 24/28 proved, 33/39 refuted, 0 wrong. The recorded run is a second, identical full run (`--write-results`): the same rewrites numbers, and one refutation fewer, because the bounded checker's counterexample search is time limited and three `DISTINCT` pairs (`aw-distinct-composite-key-part-of-key`, `aw-distinct-composite-key-detail-id-alone`, `aw-distinct-after-key-join-fan-out`) flipped between refuted and unknown on a loaded machine. The test floor is therefore set below both runs. The workload and pairs were not edited after the first run, and no rule or prover was changed. Before the first run only the pair `aw-composite-fk-join-one-column` was reshaped (it ran a join of tens of millions of rows on the real data, so it is filtered to one order); that is a test-set edit made before scoring.
- **Held out.** A fifth of the queries and a fifth of the pairs, by SHA-1 of `adventureworks:<id>`, reported apart in both results files. They were seen in the printed output of the first runs (0 wrong on all of them); nothing was tuned on them, because nothing was tuned.
- **Authored.** The pairs and 48 of the 71 workload queries were written for this eval; only the views and function and procedure bodies come from upstream, and each is adapted as recorded.
- **Not used.** The 8 XQuery views, the hierarchyid procedures, the full-text procedure, the DML procedures and triggers (above), the `DatabaseLog` table, and the other AdventureWorks releases (OLAP, data warehouse, lightweight and `.bak` files).
- **DuckDB stands in for BigQuery.** Queries are BigQuery SQL transpiled to DuckDB by sqlglot, and a rewrite is checked against a control that went through the same translation. Where the two engines differ the comparison still holds, because both sides run on DuckDB.
- **Types kept as text.** XML, GUIDs, hierarchyid and geography values are strings, so no query here exercises their behaviour.
- **Network.** The data is fetched from GitHub release assets on first use; the tests skip when it cannot be reached, so a green run without network proves less than a run with it.
