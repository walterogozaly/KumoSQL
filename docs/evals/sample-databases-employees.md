# Sample databases: Employees

Employees, the MySQL sample database of a fictional company's staff, departments, titles and salary history, loaded whole into DuckDB from its pinned upstream scripts, with the same two scores as the other [sample databases](sample-databases.md): KumoSQL's rewrites on its workload, checked on the real data, and query pairs on its schema run through the provers. It is an adapter of `tools/sample_db_bench.py` with a module of its own (`tools/sample_db_employees.py`) and its own results files. It differs from the others in two ways: **its data is downloaded at run time, never committed** (licence and size), and **its composite keys contain `DATE` columns** (`titles` and `salaries`), so its key-dependent siblings are about dates.

| Database | Upstream (pinned) | Licence | Loaded | Declared keys |
| --- | --- | --- | --- | --- |
| Employees | [datacharmer/test_db](https://github.com/datacharmer/test_db) `e324b56193ca`: `employees.sql`, `objects.sql`, the 8 `load_*.dump` scripts, `test_employees_md5.sql`, `test_employees_sha2.sql`, `README.md` (13 files, 167 MB, each SHA-256 in `tools/sample_db_employees.py`) | Creative Commons Attribution-Share Alike 3.0 (Copyright (C) 2007, 2008 MySQL AB; data by Fusheng Wang and Carlo Zaniolo, schema by Giuseppe Maxia, conversion by Patrick Crews) | 6 tables, 3,919,015 rows | 6 primary, 6 foreign keys, 23 NOT NULL columns |

| Score | Results file |
| --- | --- |
| 65 workload queries through every rewrite: **0 wrong in 448 executed cases, 206 rewrites verified on the real data** | `sample-databases-employees-rewrites` |
| 76 authored pairs: **32/35 equivalent proved, 40/41 different refuted (every refutation replayed), 0 wrong** | `sample-databases-employees-pairs` |

```
python tools/sample_db_bench.py --check --database employees       # download (first use), load and check against upstream
python tools/sample_db_bench.py --database employees --part pairs   # (b) only, a few minutes
python tools/sample_db_bench.py --database employees --write-results   # both parts and both results files, 25 to 30 minutes
python -m pytest -m slow tests/test_sample_db_employees.py          # the tests that need the data (slow tier)
```

The Employees database is not part of a default run: `ADAPTERS` holds the databases with committed data, `DOWNLOADED` the ones fetched at run time, and `--database employees` names it (`adapter_named` finds either kind). The results are kept apart from the other databases on purpose: a database that changes the numbers of rows already on the scoreboard would hide whether a rule or prover change moved them.

## Download, licence and what is committed

The data is licensed CC BY-SA 3.0, which asks that adaptations be shared alike, and it is 167 MB, so **none of the upstream files and none of the rows is in the repository**. `tools/sample_db_employees.py` downloads the 13 pinned files from `raw.githubusercontent.com/datacharmer/test_db/<commit>/` into `$KUMOSQL_BENCH_DATA/sample-db-employees/<commit>` (default `~/.cache/kumosql-bench`), checking each against its SHA-256 before it is renamed into place: a transfer cut short is retried and never cached, parallel runs never read half a file, and a cached file that does not match the pin is an error (`PinMismatch`), not a skip. When GitHub cannot be reached, `DataUnavailable` is raised and the tests that need the data skip. Those tests carry the `slow` marker, so the default and quick test runs never touch the network or build the database; the tests that need no download (the pins, the `INSERT` scanner, the checksum chain, the authored files, the results files) run in every suite.

What is committed in [`tests/fixtures/sample_databases/employees`](../../tests/fixtures/sample_databases/README.md), under the same licence and marked as adapted, is `adapted/schema.sql` (BigQuery DDL), the workload (`workload.json`, the adapted upstream queries and the authored ones) and `pairs.json`, with `NOTICE.md` giving the attribution. The Creative Commons legal code could not be fetched when the folder was written, so the licence is cited by its public address as the upstream header cites it.

## Loading and the checks against upstream

DuckDB runs the upstream `INSERT` statements as they are (MySQL's backticks removed) into a DuckDB file in the cache. It is built once (a few minutes on a shared machine, 29 MB), under a lock so that parallel workers wait for one build, and opened read-only afterwards; every worker and every test connects in a moment. No row is copied by hand. Every run checks the load, and the eval stops on any difference:

- the SHA-256 of each upstream file;
- the same tables, columns (in order), NOT NULL columns, primary keys and foreign keys as upstream's `CREATE TABLE` statements;
- each table's row count equal to the rows the scripts insert, counted by a **tokenizing scan of the scripts that does not use DuckDB** (it must account for every character of a file or it raises, and it refuses backslash escapes), and to the counts upstream's test scripts publish (300,024 employees, 9 departments, 24 managers, 331,603 department assignments, 443,308 titles, 2,844,047 salaries);
- the **MD5 and SHA-256 checksum chains that upstream's test scripts publish for every table** (`test_employees_md5.sql`, `test_employees_sha2.sql`), recomputed from the loaded rows in the order and with the value formatting of MySQL's `CONCAT_WS('#', @crc, ...)` chain (NULL skipped, dates and numbers as MySQL prints them). They match for all six tables, which says that every loaded value is the upstream value, not only that the counts agree. The chains read all 3.9 million rows (about a minute), so a database file that passed once is marked (`.verified`, keyed by the file's size and modification time) and not hashed again; deleting the marker or the file repeats the check;
- every declared key unique, every NOT NULL column without NULLs and every declared foreign key satisfied by the real rows;
- the record-count comparison of upstream's test script and the four views, which are workload queries (below).

### What the database is

- `titles.to_date` is the only nullable column, and **it has no NULL in the data** (`9999-01-01` marks an open title, as it does in `salaries` and `dept_emp`: 240,124 open rows in each). The pairs about NULL semantics therefore use witness databases, not the data.
- The composite keys are `dept_emp (emp_no, dept_no)`, `dept_manager (emp_no, dept_no)`, `titles (emp_no, title, from_date)` and `salaries (emp_no, from_date)`: two have a `DATE` column in the key, so `DISTINCT` and join elimination on a part of a key are about dates. In the data an employee holds the same title twice in 2 cases and never starts two titles on one day, so the sibling pairs that need those rows use witness databases too.
- What the BigQuery DDL leaves out: `ON DELETE CASCADE`, the `UNIQUE` key on `departments.dept_name` (BigQuery has no `UNIQUE`, so it is not declared to the provers), the engine and the character set. `gender` is an `ENUM('M','F')` loaded as its text.

## Workload (a): every rewrite, checked on the real data

| Origin | Queries | What it is |
| --- | ---: | --- |
| `upstream-view` | 4 | the 4 `CREATE VIEW` bodies of `employees.sql` and `objects.sql` (`dept_emp_latest_date`, `current_dept_emp`, `v_full_employees`, `v_full_departments`), adapted to BigQuery SQL (a view that reads another is inlined; the stored functions `v_full_*` call are inlined as correlated subqueries). The harness checks each names a view of the upstream script |
| `upstream-procedure` | 5 | the `SELECT`s of the stored functions `emp_dept_id`, `emp_dept_name`, `emp_name` and `current_manager` with their parameters bound, and of the procedure `show_departments` (its temporary tables as CTEs) |
| `upstream-test` | 1 | the record-count comparison of `test_employees_md5.sql` (expected against found counts). Its checksum probes use MySQL session variables and are checked by the harness instead |
| `authored` | 55 | written for this eval: `DISTINCT` on single and composite keys (also with a date), `IN`, `EXISTS`, `NOT EXISTS`, `NOT IN` over a nullable column, CTEs with an unused one, derived tables and trivial predicates, joins on a not-null foreign key, two parents, left joins on a key, self joins on whole and part of a composite key, `GROUP BY` a key and its dependents, `HAVING`, set operations, `RANK`, running `SUM` and `LAG` over dates, date functions and arithmetic, the `9999-01-01` sentinel, `COALESCE` and `COUNT` of the nullable column, `LIKE`, `IN`, `BETWEEN`, `CASE`, `LIMIT`, string functions |

Each adaptation is recorded with the query in `workload.json` (`adaptation`): MySQL's `set @max_date` bookkeeping in the function bodies is dropped; stored-function calls and views are inlined, because the harness keeps the loaded database read-only and creates no views or functions; `show_departments` selects non-aggregated `dept_name` and `manager` under `GROUP BY dept_no`, which MySQL accepts and BigQuery does not, so they join the `GROUP BY`; `IF` becomes `CASE`.

Each query goes through the same stages as the other sample databases (see [the first page](sample-databases.md#workload-a-every-rewrite-checked-on-the-real-data)): the canonical rule pipeline on the query and four variants, `lift_subqueries`, and the proof-gated optimizer with the declared keys. A rewrite is wrong when its result multiset differs from the unrewritten control (rerun with DuckDB's optimizer off before it counts) or when it no longer runs.

| Origin | Cases | Executed | Changed and verified | No change | Unsupported | Wrong |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| upstream views | 28 | 28 | 13 | 15 | 0 | 0 |
| upstream procedures | 34 | 34 | 14 | 20 | 0 | 0 |
| upstream test | 7 | 7 | 4 | 3 | 0 | 0 |
| authored | 379 | 379 | 175 | 204 | 0 | 0 |
| **all** | **448** | **448** | **206** | **242** | **0** | **0** |

By stage: the pipeline changed 190 of 318 cases (`format_sql` 187, `inline_single_use_ctes` 128, `remove_trivial_predicates` 66, `remove_unused_ctes` 64, `remove_redundant_parentheses` 11), `lift_subqueries` 7 of 65 and the optimizer 9 of 65. The optimizer's changes are the ones the declared keys license: it dropped a `DISTINCT` on `employees`' key (`emp-a01`) and on `titles`' composite key `(emp_no, title, from_date)` (`emp-a02`, a key with a date), a `LEFT JOIN` to `departments` on its key (`emp-a10`), and inlined CTEs, a trivial predicate and a useless `ORDER BY`. Every change returned the same rows as the control. Held out (11 queries, by SHA-1 of `employees:<query id>`): 0 wrong, 34 changed of 76 executed. No query errored, timed out or was unsupported. Each run compares results on up to 2.8 million rows, so the full run takes 25 to 30 minutes on a shared machine.

## Pairs (b): the provers on the declared keys

76 authored pairs, each labelled `equivalent` or `different` under the declared keys, in `pairs.json` with the reason. 35 equivalent pairs are rewrites that hold on this schema: join elimination through a NOT NULL foreign key (to employees, to departments, two parents at once), `LEFT JOIN` elimination on a single and a whole composite key, self joins on the whole key of `titles` (three columns, one a `DATE`) and `salaries`, `DISTINCT` removal on single and composite keys, `COUNT` of a NOT NULL column, `COUNT(DISTINCT key)`, a dependent `GROUP BY` column, grouping by a whole key as the identity, `HAVING` on the group key, filter pushdown, `IN` to `EXISTS` and to a join on a key, `NOT IN` to `NOT EXISTS` on NOT NULL columns (one a `DATE`), outer to inner join, `UNION ALL` of complementary filters, `INTERSECT` to `IN`, `IS NULL`, `COALESCE` and `NOT (a = b)` on NOT NULL columns, and DATE predicates (a year as a half-open range, `BETWEEN`). 41 are negative siblings: the same rewrite where it does not hold, 12 of them the equivalent pair itself with one guarantee removed from the declarations (`drop`: a foreign key, a primary key or a NOT NULL).

The key-dependent siblings that are particular to this schema: a self join or `DISTINCT` on `titles` without `from_date` (or on `(emp_no, from_date)` without `title`) is not the identity; on `salaries` the same with `emp_no` alone; the left join to `dept_manager` is eliminable on the whole key `(emp_no, dept_no)` and not on `dept_no`; a nullable `titles.to_date` makes `COUNT`, `COALESCE`, `NOT IN`, `INTERSECT`, a negated comparison and complementary filters differ from their forms on NOT NULL columns; DATE reaches `9999-12-31`, so `to_date = DATE '9999-01-01'` is not `to_date >= DATE '9999-01-01'`.

Every query of a pair carries a range filter on `emp_no` (about 200 employees) so that comparing the two results on the real 3.9 million rows stays small; the filter is part of both sides, and the pairs whose results are small anyway (about `departments` and `dept_manager`, or aggregates) have none. Every `different` label has its own evidence, independent of the provers: the pair returns different rows on the real data (`witness: "real"`, 15 pairs), or a small witness database separates it (26 pairs), completed with legal values and checked against the declarations that remain. An `equivalent` pair that differs on the real data would be reported as a label error; none does. Provers, in order: the structural prover, the algebraic/SMT prover with the declared constraints and its executed counterexample search, then the bounded checker (at most 2 rows per table) for a counterexample only. Every proof is checked on the real database. Every counterexample is completed with legal values, must satisfy the pair's declarations, and must separate the pair when replayed in DuckDB. All 32 proofs came from the algebraic prover; the 40 refutations are 6 algebraic and 34 bounded counterexamples.

| Category | Pairs | Equivalent proved | Different refuted |
| --- | ---: | ---: | ---: |
| join elimination | 10 | 4/4 | 6/6 |
| outer join | 7 | 3/3 | 4/4 |
| self join | 5 | 2/2 | 3/3 |
| DISTINCT removal | 12 | 5/5 | 7/7 |
| aggregation | 13 | 6/7 | 5/6 |
| subquery | 10 | 5/5 | 5/5 |
| set operation | 5 | 2/2 | 3/3 |
| NULL semantics | 6 | 3/3 | 3/3 |
| DATE | 6 | 2/3 | 3/3 |
| window | 2 | 0/1 | 1/1 |
| **all** | **76** | **32/35** | **40/41** |

0 wrong; all 12 siblings that drop a guarantee are refuted by a replayed database that keeps every other guarantee. Held out (19 pairs, by SHA-1 of `employees:<pair id>`): 7/8 proved, 11/11 refuted, 0 wrong.

Unknown (4):

- not proved (3): a `LEFT JOIN` count against the correlated `COUNT(*)` subquery (the algebraic prover does not support that subquery shape), the `9999-01-01` sentinel written as an exclusive range (`> 9998-12-31 AND <= 9999-01-01`, "no row-preserving mapping") and a filter on a window's partition column pushed inside the subquery (held out);
- not refuted (1): `COUNTIF(p)` against a `WHERE p` filter, whose difference is a group that exists only in one query. Its label is verified by a witness database; no prover produced a counterexample.

## Prover bugs: none found

The bounded checker used to clamp a `DATE` model value below year 1 to `0001-01-01`, so two rows that differ only in a `DATE` key column came out equal and the counterexample repeated the key (the pairs on `titles` and `salaries` are about exactly that). That bug is fixed on master (the range is now a constraint, see [bounded verification](bounded-verification.md)) and this adapter ran on the fixed code: none of the 76 pairs hit it, and the siblings that need two rows with different `DATE` key values are refuted by counterexamples that replay. The replay gate and its `known_prover_bug` annotation (see [Pagila](sample-databases-pagila.md#bugs-found)) are in force for this adapter, and no pair needs the annotation. No prover module was changed for this eval.

## Baseline, held-out cases and limits

- **Baseline.** The first full run is the rewrites score above (nothing tuned, no rule changed). The first pairs run was 32/35 proved and 39/41 refuted with **1 wrong, which was the author's label, not a prover**: `emp-intersect-to-in-nullable` was labelled `different` because `titles.to_date` is nullable, but the other side of its `INTERSECT` was `salaries.to_date`, which is NOT NULL, so no NULL can meet another, the pair is equivalent and the algebraic prover's proof was right (the harness reported "label unverified" for the same reason: its witness did not separate). The pair now intersects `titles.to_date` with itself and is refuted. Two more labels were reported unverified and their witnesses corrected: `emp-not-in-nullable` (the witness used `emp_no` 10100, which the query's own filter `emp_no < 10100` excludes) and `emp-filter-vs-conditional-count` (it was labelled `real`, but the data has no department whose assignments have all ended; it now has a witness database). The last rerun of those three pairs is what the recorded scores contain; nothing else changed between the runs.
- **Tuned on test, partly.** `emp-not-in-nullable` is a held-out pair, so its unverified label was seen in the printed output before its witness was fixed (a witness defect, not a rule or prover change). The other held-out pairs and the held-out queries were seen only as 0-wrong results. No rule, prover or harness decision was tuned on them.
- **Held out.** A fifth of the queries and a fifth of the pairs, by SHA-1 of `employees:<id>`, reported apart in both results files.
- **Authored.** The pairs and 55 of the 65 workload queries were written for this eval; only the views, function bodies, the procedure and the test script's comparison come from upstream, and each is adapted as recorded. The upstream test script's checksum probes are used as load checks, not as workload.
- **Not used.** `employees_partitioned.sql` (a partitioned variant of the same schema), `test_employees_sha.sql` (the SHA-1 version of the checksum test; the MD5 and SHA-256 chains are used), the `postgresql/` port, and the `sakila/` folder (the Sakila adapter's data, committed there under its own licence).
- **Data volume.** The results are produced on 3.9 million rows, and a query that fans out on `salaries` can return millions of rows, so the pair queries carry the range filter described above. The first build and download of the data take a few minutes; the full run, 25 to 30 minutes on a shared machine, is the slowest of the sample databases.
- **DuckDB stands in for BigQuery.** Queries are BigQuery SQL transpiled to DuckDB by sqlglot, and a rewrite is checked against a control that went through the same translation.
- **Overlap.** No other eval in this repository uses Employees. Sakila, which lives in the same upstream repository, is a separate database with its own adapter and committed data.
- **Undeclared uniqueness.** `departments.dept_name` is unique upstream but not declared (BigQuery has no `UNIQUE`), so a pair that needs it is out of reach for the provers here; none is scored.
