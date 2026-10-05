# Checking rewrites and proofs on real sample databases

[Simple eval index](README.md) · [Full reference](../../docs/evals/sample-databases.md)

Most evals use small invented tables. This one loads three complete public sample databases, Chinook (a music store), Northwind (a trading company) and Sakila (a DVD rental shop), into DuckDB from their pinned upstream scripts. They come with real keys, foreign keys, NOT NULL columns, thousands of rows, and Northwind's and Sakila's own views and stored procedures. Chinook and Northwind share one pair of recorded scores; Sakila has its own, so adding a database never changes the numbers of another. A fourth database, Pagila (a PostgreSQL DVD-rental sample), has [its own page](sample-databases-pagila.md) and its own results files; Oracle's Human Resources and Customer Orders schemas follow, each with its own results files.

## What it checks

- **Rewrites on real data.** Each workload query goes through KumoSQL's rewrite rules. A rewrite counts as wrong only if the rewritten query returns different rows on the real database than the original, confirmed with DuckDB's optimizer switched off.
- **Query pairs.** Hand-written pairs on the same schemas go through the provers: the ones that really are equal should be proved, and the ones that really differ should be refuted with a database that DuckDB replays. Many pairs come in siblings: the same rewrite with and without a foreign key, primary key or NOT NULL guarantee. The sibling without the guarantee must not be proved.

## A concrete example

`SELECT c.CustomerId FROM Customer c JOIN Invoice i ON i.CustomerId = c.CustomerId WHERE i.Total > 5` and the `IN (SELECT ...)` form return the same customers only if each customer appears once per match. The eval checks that the prover's answer agrees with what DuckDB returns on the real Chinook rows.

Sakila adds shapes the first two lack: a table whose primary key is two columns (which film an actor played in), two foreign keys from one table to another, a foreign key that is empty in a few real rows (a payment with no rental) and one that is empty in every row, and a loop of required foreign keys between `store` and `staff`. Its seven views and the SELECTs of its stored routines are in the workload, rewritten from MySQL into BigQuery SQL with every change written beside the query. The same rewrite is asked about with and without the guarantee it needs: `SELECT r.rental_id FROM rental r JOIN inventory i ON i.inventory_id = r.inventory_id` equals `SELECT rental_id FROM rental` only because `rental.inventory_id` is required and points at inventory's key.

## The Oracle schemas

Oracle's two small sample schemas are Human Resources (employees, departments, jobs, locations, a manager column that points back at employees, and a job history keyed by employee and start date) and Customer Orders (customers, stores, products, orders, shipments, order items keyed by order and line number, and inventory). Their scripts are written for Oracle, so the harness rewrites the table definitions into BigQuery's dialect (`NUMBER` becomes `INT64` or `NUMERIC`, `VARCHAR2` becomes `STRING`) and drops what BigQuery cannot declare: CHECK and UNIQUE constraints, identity columns, sequences, indexes, triggers. The rows are read straight from Oracle's own `INSERT` scripts, including its `TO_DATE` calls and one JSON text that the script builds in two pieces.

A concrete example from Customer Orders: `SELECT DISTINCT order_id, line_item_id FROM order_items` is the same as the query without `DISTINCT` because that pair of columns is the primary key. `SELECT DISTINCT order_id, product_id FROM order_items` is not provably the same: Oracle makes `(product_id, order_id)` unique, but BigQuery cannot declare that, so the checker does not know it, and the pair is labelled "different" under the declared keys even though the real rows agree.

Each Oracle schema has its own two results files, so the numbers of the other databases stay as they were. Everything is checked the same way: every rewrite on the real rows, every proof against the real data, every counterexample replayed on a legal database. The first run counted three Human Resources pairs as wrong, not because a proof was false but because one of the bounded checker's counterexamples put the same date into two rows that must have different dates, which breaks the declared primary key. That bug was fixed in the checker separately (this eval changed no prover); the pairs were rerun unchanged and are refuted correctly, so the recorded Oracle scores have no wrong answers and no list of known failures.

## Oracle Sales History

Sales History is Oracle's third sample schema and the first big one: a "star" of sales and costs records (918,843 sales) around small tables for days, products, customers, countries, sales channels and promotions. Its rows are six CSV files, 91 MB, too big to keep in the repository, so the eval downloads them when it runs, from the same pinned upstream version, and refuses any file whose fingerprint (SHA-256) is not the recorded one. If GitHub cannot be reached the tests skip rather than fail, and nothing is invented in place of the data. The small SQL scripts and the licence are kept unchanged in the repository, as for the other two Oracle schemas.

A concrete example: summing sales per day and then adding the days up per month gives the same monthly totals as summing the sales per month directly, so the pair is "equal" and the prover should prove it. Averaging the daily averages is not the same (a day with two sales counts as much as a day with one), so that pair is "different" and the eval wants a database where the two answers part, which is replayed in DuckDB. Joining sales to the products table can be dropped only because every sale names a product (a required foreign key) and each product appears once (a primary key); the sibling pairs take either guarantee away, and the prover must not prove them.

The scores are in two results files of their own (`sample-databases-oracle_sh-rewrites` and `-pairs`), so no other database's numbers moved. Its first run had two "different" labels that the real data did not support; they were given a small hand-built database as proof, and the time limit for the one query that joins the two big fact tables was raised, before anything else changed. No rewrite rule or prover was touched. The scores and the held-out results are in the [full reference](../../docs/evals/sample-databases.md#oracle-sales-history-run-time-download).

Left out on purpose: IBM's FIBEN (152 tables, an 80 MB archive).

## Limits of the evidence

- The query workload is small and partly written for this eval, so a good score shows the rules behave on these databases, not on every database.
- Upstream queries are adapted from T-SQL or SQLite to BigQuery SQL, and DuckDB stands in for BigQuery as the oracle.
- A bounded counterexample must fit the declared column types. A `NUMERIC(10, 2)` column holds two decimal digits, so a difference that only shows with a third digit is not a counterexample.
- Sakila's files come from a mirror and are Sakila Spatial 0.9, not the 1.2 files of the MySQL download site, which cannot be reached from the test machines. The two UNIQUE keys Sakila declares are not given to the provers.
- Sakila's first run counted 23 counterexamples as wrong. Twenty-two were a gap in the bounded checker (it cannot invent a value for a binary column) that the harness now fills in; one pair, whose counterexample cannot be turned into a legal database because of the store and staff loop, is listed but not scored. The details, and why this is not a clean held-out result, are in the full reference.
- Oracle's UNIQUE and CHECK constraints are not given to the provers (BigQuery cannot declare them), so a pair that depends on one is labelled "different" under the declared keys even though the real rows agree.
- The held-out queries and pairs (chosen by hash of their id) are run once and reported apart.

The recorded scores, the pinned upstream versions, licences, and every adaptation are in the [full reference](../../docs/evals/sample-databases.md) and the `sample-databases-*` results files (two each for Chinook and Northwind together, Sakila, Pagila, Oracle HR, Oracle Customer Orders and Oracle Sales History).

```sh
python tools/sample_db_bench.py --check
python tools/sample_db_bench.py --part pairs
python tools/sample_db_bench.py --database sakila --part pairs
python tools/sample_db_bench.py --database oracle_hr --write-results
python tools/sample_db_bench.py --database oracle_sh --write-results   # downloads 91 MB once
```
