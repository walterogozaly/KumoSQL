# Generating tests to find unsafe changes

[Simple eval index](README.md) · [Full reference](../../docs/evals/fuzzing.md)

Fuzzing creates many queries and data shapes to look for mistakes. Here the generator is seeded: using the same seed reproduces the same cases.

## Three kinds of tests

- **Metamorphic tests** compare different ways to compute the same result. For example, splitting rows into “condition true,” “condition false,” and “condition NULL,” then recombining them should recover all rows.
- **Unsafe variants** make tempting changes such as dropping a duplicate-producing join or changing a NULL-sensitive filter.
- **Composition tests** apply several rewrite rules in sequence and check that no step changes results.

A fourth, typed soundness fuzzer tests proof claims on schemas with types and integrity constraints. Its found false proofs are soundness bugs; read the full guide's current findings before relying on the affected shapes.

NULLs need the third partition: in SQL, a condition can be unknown as well as true or false.

## Run a sample

```sh
python tools/unsafe_fuzz.py fuzz --count 60 --seed 1
```

Run from a development checkout. The full guide supplies unsafe and composition commands.

The suite checks proofs against small DuckDB databases and replays counterexamples. It distinguishes false proofs, counterexamples that fail to show a difference, bugs in generated labels, and behavior-changing rule steps.

Finding no difference on the generated databases is executed evidence. It does not establish unbounded equivalence. Coverage and wrong-answer counts are separate so an unknown answer is not mistaken for a false proof.

## Queries that cannot return a row

Some generated pairs differ by a small edit (a constant, a dropped `NOT`) inside a query that returns nothing whatever the data. Example: a derived table sums `b` over rows where `b >= a`; those rows all have a real `b`, so the sum is never NULL, and an outer filter `WHERE sum IS NULL` can never be true. Both versions of such a query are empty, so they are equivalent. KumoSQL tracks which columns are always NULL or never NULL, and when a filter contradicts that, it replaces the filter by `FALSE`.

It is careful about the cases that look similar but are not empty: a sum over no rows with no `GROUP BY` is one NULL row, `ROLLUP` and `CUBE` add a total row, a `LEFT JOIN` can pad a column with NULL, and `COUNT` is never NULL. If it cannot tell, it leaves the query alone.

The evidence is the unit tests, with near misses that really return a row, plus a random comparison of rewritten and original queries on small databases. That is evidence, not a proof of the rule. See the [full reference](../../docs/evals/fuzzing.md#queries-that-can-never-return-a-row-null-facts) for the exact conditions.
