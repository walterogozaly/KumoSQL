# Pagila: a PostgreSQL sample database

[Simple eval index](README.md) · [Full reference](../../docs/evals/sample-databases-pagila.md) · [Sample databases overview](sample-databases.md)

Pagila is PostgreSQL's DVD-rental sample database: films, actors, customers, rentals and payments. This eval loads the whole thing, about 122,000 rows in 16 tables, into DuckDB from its pinned upstream scripts and asks the same two questions as the Chinook and Northwind eval. It has its own results files, so the first two databases' numbers stay as they were.

## What it checks

- **Rewrites on real data.** Pagila's own views, the `SELECT`s inside its functions, the queries in its README and some written for this eval go through KumoSQL's rewrite rules. A rewrite is wrong only if the rewritten query returns different rows on the real data than the original.
- **Query pairs.** Hand-written pairs go through the provers. The pairs that really are equal should be proved, and the ones that really differ should be refuted with a small database that DuckDB replays. Many pairs are siblings: the same rewrite with and without a key, foreign key or NOT NULL guarantee. The sibling without the guarantee must not be proved.

## A concrete example

`SELECT DISTINCT actor_id, film_id FROM film_actor` can lose its `DISTINCT`, because `(actor_id, film_id)` is the table's key. `SELECT DISTINCT film_id FROM film_actor` cannot, because a film has several actors. In the same way `payment` is keyed by `(payment_date, payment_id)`, so `SELECT DISTINCT payment_id, amount FROM payment` keeps its `DISTINCT` under the declarations, even though no two payments in the data share an id.

## What the eval found

- Pagila declares `payment`'s foreign keys on only six of its 55 monthly partitions. The table as a whole therefore declares none, and a join from `payment` to `customer` cannot be removed under the declarations although the data would allow it.
- `film.original_language_id` is empty in every film, which makes joins and `NOT IN` on it good traps.
- The bounded checker has a bug: it can produce a counterexample whose two rows have the same date, which breaks a key that includes a date. The replay step catches it, so the two affected pairs stay "unknown" and are listed as prover bugs rather than counted as wrong answers.

## Limits of the evidence

- The pairs and most of the workload are written for this eval, so a good score shows the rules behave on this one database, not on every database.
- Upstream queries are adapted from PostgreSQL to BigQuery SQL, and DuckDB stands in for BigQuery as the oracle. Types BigQuery lacks (arrays, vectors, full-text, UUIDs) are loaded as text.
- A fifth of the queries and pairs, chosen by hash of their id, are held out and reported apart. Nothing was tuned on them.

The recorded scores, the pinned upstream commit, licence and every adaptation are in the [full reference](../../docs/evals/sample-databases-pagila.md) and the two `sample-databases-pagila-*` results files.

```sh
python tools/sample_db_bench.py --check --database pagila
python tools/sample_db_bench.py --database pagila --part pairs
```
