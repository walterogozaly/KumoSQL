# Checking rewrites and proofs on real sample databases

[Simple eval index](README.md) · [Full reference](../../docs/evals/sample-databases.md)

Most evals use small invented tables. This one loads complete public sample databases into DuckDB from their pinned upstream scripts: Chinook (a music store), Northwind (a trading company), and Oracle's Human Resources and Customer Orders schemas. They come with real keys, foreign keys, NOT NULL columns, thousands of rows, and the views and stored procedures their authors shipped.

## What it checks

- **Rewrites on real data.** Each workload query goes through KumoSQL's rewrite rules. A rewrite counts as wrong only if the rewritten query returns different rows on the real database than the original, confirmed with DuckDB's optimizer switched off.
- **Query pairs.** Hand-written pairs on the same schemas go through the provers: the ones that really are equal should be proved, and the ones that really differ should be refuted with a database that DuckDB replays. Many pairs come in siblings: the same rewrite with and without a foreign key, primary key or NOT NULL guarantee. The sibling without the guarantee must not be proved.

## A concrete example

`SELECT c.CustomerId FROM Customer c JOIN Invoice i ON i.CustomerId = c.CustomerId WHERE i.Total > 5` and the `IN (SELECT ...)` form return the same customers only if each customer appears once per match. The eval checks that the prover's answer agrees with what DuckDB returns on the real Chinook rows.

## The Oracle schemas

Oracle's two small sample schemas are Human Resources (employees, departments, jobs, locations, a manager column that points back at employees, and a job history keyed by employee and start date) and Customer Orders (customers, stores, products, orders, shipments, order items keyed by order and line number, and inventory). Their scripts are written for Oracle, so the harness rewrites the table definitions into BigQuery's dialect (`NUMBER` becomes `INT64` or `NUMERIC`, `VARCHAR2` becomes `STRING`) and drops what BigQuery cannot declare: CHECK and UNIQUE constraints, identity columns, sequences, indexes, triggers. The rows are read straight from Oracle's own `INSERT` scripts, including its `TO_DATE` calls and one JSON text that the script builds in two pieces.

A concrete example from Customer Orders: `SELECT DISTINCT order_id, line_item_id FROM order_items` is the same as the query without `DISTINCT` because that pair of columns is the primary key. `SELECT DISTINCT order_id, product_id FROM order_items` is not provably the same: Oracle makes `(product_id, order_id)` unique, but BigQuery cannot declare that, so the checker does not know it, and the pair is labelled "different" under the declared keys even though the real rows agree.

The Oracle results are kept in their own two results files so the Chinook and Northwind numbers stay as they were. Everything is checked the same way: every rewrite on the real rows, every proof against the real data, every counterexample replayed on a legal database. One thing differs from the other databases: three pairs are counted as wrong, not because a proof was false but because one of the checker's counterexamples puts the same date into two rows that must have different dates, so it breaks the declared primary key. The pairs are labelled correctly and none is proved; the bug is in the bounded checker and is reported, not fixed, here.

Left out on purpose: Oracle's Sales History (91 MB of CSV, almost a million sales rows, too big to keep in the repository) and IBM's FIBEN (152 tables, an 80 MB archive).

## Limits of the evidence

- The query workload is small and partly written for this eval, so a good score shows the rules behave on these four databases, not on every database.
- Upstream queries are adapted from T-SQL or SQLite to BigQuery SQL, and DuckDB stands in for BigQuery as the oracle.
- A bounded counterexample must fit the declared column types. A `NUMERIC(10, 2)` column holds two decimal digits, so a difference that only shows with a third digit is not a counterexample.
- The held-out queries and pairs (chosen by hash of their id) are run once and reported apart.

The recorded scores, the pinned upstream versions, licences, and every adaptation are in the [full reference](../../docs/evals/sample-databases.md) and the `sample-databases-*` results files (two for Chinook and Northwind, two for the Oracle schemas).

```sh
python tools/sample_db_bench.py --check
python tools/sample_db_bench.py --part pairs
python tools/sample_db_bench.py --group oracle --write-results
```
