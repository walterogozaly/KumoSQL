# Checking rewrites and proofs on real sample databases

[Simple eval index](README.md) · [Full reference](../../docs/evals/sample-databases.md)

Most evals use small invented tables. This one loads three complete public sample databases, Chinook (a music store), Northwind (a trading company) and Sakila (a DVD rental shop), into DuckDB from their pinned upstream scripts. They come with real keys, foreign keys, NOT NULL columns, thousands of rows, and Northwind's and Sakila's own views and stored procedures. Chinook and Northwind share one pair of recorded scores; Sakila has its own, so adding a database never changes the numbers of another. A fourth database, Pagila (a PostgreSQL DVD-rental sample), has [its own page](sample-databases-pagila.md) and its own results files.

## What it checks

- **Rewrites on real data.** Each workload query goes through KumoSQL's rewrite rules. A rewrite counts as wrong only if the rewritten query returns different rows on the real database than the original, confirmed with DuckDB's optimizer switched off.
- **Query pairs.** Hand-written pairs on the same schemas go through the provers: the ones that really are equal should be proved, and the ones that really differ should be refuted with a database that DuckDB replays. Many pairs come in siblings: the same rewrite with and without a foreign key, primary key or NOT NULL guarantee. The sibling without the guarantee must not be proved.

## A concrete example

`SELECT c.CustomerId FROM Customer c JOIN Invoice i ON i.CustomerId = c.CustomerId WHERE i.Total > 5` and the `IN (SELECT ...)` form return the same customers only if each customer appears once per match. The eval checks that the prover's answer agrees with what DuckDB returns on the real Chinook rows.

Sakila adds shapes the first two lack: a table whose primary key is two columns (which film an actor played in), two foreign keys from one table to another, a foreign key that is empty in a few real rows (a payment with no rental) and one that is empty in every row, and a loop of required foreign keys between `store` and `staff`. Its seven views and the SELECTs of its stored routines are in the workload, rewritten from MySQL into BigQuery SQL with every change written beside the query. The same rewrite is asked about with and without the guarantee it needs: `SELECT r.rental_id FROM rental r JOIN inventory i ON i.inventory_id = r.inventory_id` equals `SELECT rental_id FROM rental` only because `rental.inventory_id` is required and points at inventory's key.

## Limits of the evidence

- The query workload is small and partly written for this eval, so a good score shows the rules behave on these two databases, not on every database.
- Upstream queries are adapted from T-SQL or SQLite to BigQuery SQL, and DuckDB stands in for BigQuery as the oracle.
- A bounded counterexample must fit the declared column types. A `NUMERIC(10, 2)` column holds two decimal digits, so a difference that only shows with a third digit is not a counterexample.
- Sakila's files come from a mirror and are Sakila Spatial 0.9, not the 1.2 files of the MySQL download site, which cannot be reached from the test machines. The two UNIQUE keys Sakila declares are not given to the provers.
- Sakila's first run counted 23 counterexamples as wrong. Twenty-two were a gap in the bounded checker (it cannot invent a value for a binary column) that the harness now fills in; one pair, whose counterexample cannot be turned into a legal database because of the store and staff loop, is listed but not scored. The details, and why this is not a clean held-out result, are in the full reference.
- The held-out queries and pairs (chosen by hash of their id) are run once and reported apart.

The recorded scores, the pinned upstream versions, licences, and every adaptation are in the [full reference](../../docs/evals/sample-databases.md) and the `sample-databases-*` results files.

```sh
python tools/sample_db_bench.py --check
python tools/sample_db_bench.py --part pairs
python tools/sample_db_bench.py --database sakila --part pairs
```
