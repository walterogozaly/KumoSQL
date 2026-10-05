# Checking pairs of queries from other databases' own tests

[Simple eval index](README.md) · [Full reference](../../docs/evals/engine-paired-tests.md)

Database projects test themselves with pairs of queries: "this query must return the same rows as that simpler one". Trino does it for joins, Spark for `EXISTS` and `IN`, and PostgreSQL added one for a join-removal bug. This evaluation takes 157 such pairs, adds a few guards we wrote (a counterexample from the JoinEquiv paper and the rewrites the jOOQ manual describes, with the cases where they break), labels each pair by hand and asks KumoSQL to prove the equal ones and to find a database that separates the others.

An example: `a JOIN b USING (k)` against `a JOIN b ON a.k = b.k`. These are equal, so the pair must be proved. Another: `NOT EXISTS` against `NOT IN` look alike but differ as soon as the subquery holds a NULL, so that pair must never be proved, and a database with a NULL should show the difference.

A pair has three possible labels: equal on every database, equal only on the test's own data (for example "no part is named 'a'"), or not equal. Only a proof of the first kind and a verified difference for the others count as correct. A proof of a pair that is not equal is wrong, and so is any claim that an equal pair differs. A claimed difference only counts when its database respects the declared keys and NOT NULL columns and DuckDB, with its optimizer off, returns different rows.

```sh
python tools/engine_pairs_bench.py
```

Run from a development checkout; it needs `tpchgen-cli` for the Trino pairs. Most pairs stay unknown, mainly Trino's inline `VALUES` tables, which the prover does not yet read. The upstream queries are translated to BigQuery SQL and were run on DuckDB, not on the original engines. Every case was seen while building the harness, so the held-out fifth is not a clean test. The full reference lists the scores, the pinned upstream versions and licences, and what is proved, refuted and unknown.
