# Checking rewrites and proofs on real sample databases

[Simple eval index](README.md) · [Full reference](../../docs/evals/sample-databases.md)

Most evals use small invented tables. This one loads two complete public sample databases, Chinook (a music store) and Northwind (a trading company), into DuckDB from their pinned upstream scripts. They come with real keys, foreign keys, NOT NULL columns, thousands of rows, and Northwind's own views and stored procedures. A third database, Pagila (a PostgreSQL DVD-rental sample), has [its own page](sample-databases-pagila.md) and its own results files.

## What it checks

- **Rewrites on real data.** Each workload query goes through KumoSQL's rewrite rules. A rewrite counts as wrong only if the rewritten query returns different rows on the real database than the original, confirmed with DuckDB's optimizer switched off.
- **Query pairs.** Hand-written pairs on the same schemas go through the provers: the ones that really are equal should be proved, and the ones that really differ should be refuted with a database that DuckDB replays. Many pairs come in siblings: the same rewrite with and without a foreign key, primary key or NOT NULL guarantee. The sibling without the guarantee must not be proved.

## A concrete example

`SELECT c.CustomerId FROM Customer c JOIN Invoice i ON i.CustomerId = c.CustomerId WHERE i.Total > 5` and the `IN (SELECT ...)` form return the same customers only if each customer appears once per match. The eval checks that the prover's answer agrees with what DuckDB returns on the real Chinook rows.

## Limits of the evidence

- The query workload is small and partly written for this eval, so a good score shows the rules behave on these two databases, not on every database.
- Upstream queries are adapted from T-SQL or SQLite to BigQuery SQL, and DuckDB stands in for BigQuery as the oracle.
- A bounded counterexample must fit the declared column types. A `NUMERIC(10, 2)` column holds two decimal digits, so a difference that only shows with a third digit is not a counterexample.
- The held-out queries and pairs (chosen by hash of their id) are run once and reported apart.

The recorded scores, the pinned upstream versions, licences, and every adaptation are in the [full reference](../../docs/evals/sample-databases.md) and the two `sample-databases-*` results files.

```sh
python tools/sample_db_bench.py --check
python tools/sample_db_bench.py --part pairs
```
