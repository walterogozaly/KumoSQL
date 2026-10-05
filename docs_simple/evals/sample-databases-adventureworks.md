# AdventureWorks: a bicycle company's database

[Simple eval index](README.md) · [Full reference](../../docs/evals/sample-databases-adventureworks.md) · [Sample databases overview](sample-databases.md)

AdventureWorks is Microsoft's sample database for a bicycle manufacturer: people, employees, products, sales orders, purchase orders and bills of materials. It has 70 tables and about 760,000 rows. This eval loads all of it into DuckDB and asks the same two questions as the other sample-database evals. It is the largest of them, so its data is not stored in this repository: it is downloaded from Microsoft's GitHub release the first time the eval runs, and checked against a fingerprint (SHA-256) of every file. If the download is not possible, its tests skip instead of failing.

## What it checks

- **Rewrites on real data.** AdventureWorks' own views, the `SELECT`s inside its functions and recursive procedures, and queries written for this eval go through KumoSQL's rewrite rules. A rewrite is wrong only if the rewritten query returns different rows on the real data than the original.
- **Query pairs.** Hand-written pairs go through the provers. The pairs that really are equal should be proved, and the ones that really differ should be refuted with a small database that DuckDB replays. Many pairs are siblings: the same rewrite with and without a key, foreign key or NOT NULL guarantee. The sibling without the guarantee must not be proved.

## A concrete example

Every line of a sales order points at an order header, and the foreign key cannot be empty, so joining `SalesOrderDetail` to `SalesOrderHeader` and then selecting only detail columns changes nothing, and the join can go. For `Customer` and `Store` the same join cannot go: individual customers (18,484 of 19,820) have no store, so the join drops them. It is only equal to "customers whose store is not empty".

## What the eval found

- The views are written in SQL Server's T-SQL. Twelve adapted cleanly into BigQuery SQL and became workload queries. Eight read XML columns with XQuery and were left out, with the reason recorded: BigQuery has no equivalent, and a made-up stand-in would not be Microsoft's query.
- Some adaptations change the form but not the answer: a `PIVOT` becomes a set of conditional sums, `+` on text becomes `||`, and a recursive bill-of-materials query becomes `WITH RECURSIVE`.
- The data is the 2022 release, so one view pivots the fiscal years 2002 to 2004 and returns only empty columns, and the bill of materials is dated 2021 while orders are dated 2022 to 2025. The eval uses dates that fit.
- Some columns are unique upstream (for example a product's name) but BigQuery cannot declare uniqueness, so the provers rightly do not treat them as keys.
- No prover bug showed up on this database, including for a key that contains a date.

## Limits of the evidence

- The pairs and most of the workload are written for this eval, so a good score shows the rules behave on this one database, not on every database.
- Queries are adapted from T-SQL to BigQuery SQL, and DuckDB stands in for BigQuery as the oracle. XML, GUIDs, hierarchy ids and geography values are loaded as plain text.
- A fifth of the queries and pairs, chosen by hash of their id, are held out and reported apart. Nothing was tuned on them.
- Unknown is not wrong: some true equalities are not proved, and some real differences are not found by the provers themselves. Those stay unknown and are listed.

The recorded scores, the pinned release, licence and every adaptation are in the [full reference](../../docs/evals/sample-databases-adventureworks.md) and the two `sample-databases-adventureworks-*` results files.

```sh
python tools/sample_db_bench.py --check --database adventureworks
python tools/sample_db_bench.py --database adventureworks --part pairs
```
