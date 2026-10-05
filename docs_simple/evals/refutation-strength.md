# Can KumoSQL show that two queries differ?

[Simple eval index](README.md) · [Full reference](../../docs/evals/refutation-strength.md)

Most evals ask whether KumoSQL wrongly says two queries are the same. This one asks the reverse: for pairs that are known to return different rows, can KumoSQL build a small database that shows it? A database that makes the two queries disagree is stronger than "not proven": anyone can run both queries on it and see.

## An example

`SELECT a FROM t WHERE a NOT IN (SELECT b FROM u)` and the `NOT EXISTS` form of it are not the same: when `u.b` holds a NULL, the first returns no rows and the second returns every row. The refuter builds a table `t` with one row and a table `u` with a NULL, runs both queries, and returns that database. Some pairs need far more rows. Two queries that differ only when a group has more than 1,000 rows need a database of that size, which a separate search with row copy counts builds.

## How a pair is scored

Each pair comes with a witness database, and every run first confirms that the pair really differs on it. KumoSQL's prover then tries the pair. If it returns a database, the harness replays that database on its own and counts the pair as refuted only if the queries differ there. Proving a pair that differs is a bug and counts as wrong. Anything else is unknown.

```sh
python tools/refutation_strength_bench.py --show unknown
```

The pairs come from three places: an outside research assistant's sweep of differing pairs, the [optimizer bug](optimizer-bugs.md) pairs, and four VeriEQL pairs, two of which need more than 1,000 rows. The same run also checks 108 harder mutants of the targeted-data eval and 340 unsafe rewrites. The VeriEQL pairs need a download; without it that source is skipped.

## Limits of the evidence

* Most pairs were seen while building the refuter, so the score shows what it does on pairs it was built around. Only five optimizer-bug pairs were held out.
* The pairs are small and chosen to differ on empty tables, NULLs or duplicates.
* The queries are run on DuckDB, so a difference is a claim about DuckDB's reading of them. BigQuery SQL goes through a layer that gives no answer where the two engines would diverge.
* Two pairs are not counted because their difference rests on an arbitrary pick or an approximation, and a couple more are not refuted.
* "0 wrong" covers these pairs; it does not show the refuter never contradicts a true equivalence.

The recorded scores are in the [full reference](../../docs/evals/refutation-strength.md#scores).
