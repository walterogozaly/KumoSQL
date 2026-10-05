# Generating tests to find unsafe changes

[Simple eval index](README.md) · [Full reference](../../docs/evals/fuzzing.md)

Fuzzing creates many queries and data shapes to look for mistakes. Here the generator is seeded: using the same seed reproduces the same cases.

## Three kinds of tests

- **Metamorphic tests** compare different ways to compute the same result. For example, splitting rows into “condition true,” “condition false,” and “condition NULL,” then recombining them should recover all rows.
- **Unsafe variants** make tempting changes such as dropping a duplicate-producing join or changing a NULL-sensitive filter.
- **Composition tests** apply several rewrite rules in sequence and check that no step changes results.

A fourth, typed soundness fuzzer tests proof claims on schemas with types and integrity constraints. Its found false proofs are soundness bugs; read the full guide's current findings before relying on the affected shapes.

NULLs need the third partition: in SQL, a condition can be unknown as well as true or false.

## Queries with arrays and structs

Some generated queries use BigQuery constructs that the prover cannot read directly. Three small rewrites turn them into plain SQL first:

- A struct column that is only read field by field (`s.f`) is replaced by one ordinary column per field read.
- `CROSS JOIN UNNEST([x.a, x.b, 1])` becomes three copies of the query, one per array element, glued with `UNION ALL`.
- A filter inside an `IN (...)` subquery that only throws away NULL candidates is dropped, because a NULL candidate can never make `IN` true.

For example, `SELECT s.f FROM (SELECT STRUCT(x.a AS f, x.b AS g) AS s FROM t AS x)` is read as `SELECT x.a FROM t AS x`. Each rewrite only fires on the exact shapes it has been checked for: a struct read as a whole, an array with a NULL element, `NOT IN`, or an unread aggregate that decides how many rows come back are all left alone, and the pair stays unknown.

One generated pair changes a constant (`BETWEEN 0 AND 3` to `BETWEEN 0 AND 4`) and the random databases never reach the value that tells the two apart. The refutation search now also tries small databases built from the numbers written in the queries, finds it and reports the pair as different.

The evidence is the generated databases and the unit tests, not a proof of the rewrites themselves. Details: [full reference](../../docs/evals/fuzzing.md#bigquery-constructs-struct-fields-unnest-of-literals-null-candidates).

## Run a sample

```sh
python tools/unsafe_fuzz.py fuzz --count 60 --seed 1
```

Run from a development checkout. The full guide supplies unsafe and composition commands.

The suite checks proofs against small DuckDB databases and replays counterexamples. It distinguishes false proofs, counterexamples that fail to show a difference, bugs in generated labels, and behavior-changing rule steps.

Finding no difference on the generated databases is executed evidence. It does not establish unbounded equivalence. Coverage and wrong-answer counts are separate so an unknown answer is not mistaken for a false proof.
