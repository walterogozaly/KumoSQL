# Generating tests to find unsafe changes

[Simple eval index](README.md) · [Full reference](../../docs/evals/fuzzing.md)

Fuzzing creates many queries and data shapes to look for mistakes. Here the generator is seeded: using the same seed reproduces the same cases.

## Three kinds of tests

- **Metamorphic tests** compare different ways to compute the same result. For example, splitting rows into “condition true,” “condition false,” and “condition NULL,” then recombining them should recover all rows.
- **Unsafe variants** make tempting changes such as dropping a duplicate-producing join or changing a NULL-sensitive filter.
- **Composition tests** apply several rewrite rules in sequence and check that no step changes results.

A fourth, typed soundness fuzzer tests proof claims on schemas with types and integrity constraints. Its found false proofs are soundness bugs; read the full guide's current findings before relying on the affected shapes.

NULLs need the third partition: in SQL, a condition can be unknown as well as true or false.

## Splitting a DISTINCT query by a condition

Another metamorphic test splits `SELECT DISTINCT x FROM t` into three `UNION DISTINCT` pieces: rows where a condition is true, false, or NULL. KumoSQL now merges pieces of the same query that differ only in their filters back into one piece, because a set that removes duplicates does not care which piece a row came from. If the three filters together cover every row, the filter disappears and the pair is proved equal.

Example: `SELECT a FROM t WHERE b > 0 UNION DISTINCT SELECT a FROM t WHERE NOT (b > 0) UNION DISTINCT SELECT a FROM t WHERE (b > 0) IS NULL` is `SELECT DISTINCT a FROM t`. Drop the NULL piece and it is not: rows where `b` is NULL disappear, and the prover says so.

Limits: the rule only merges simple pieces (no aggregates, windows, LIMIT, `DISTINCT ON`, or random functions), and it was checked on generated tables and a rewrite-level fuzz, not proved for every database. See the [full reference](../../docs/evals/fuzzing.md) for the rule details and recorded scores.

## Run a sample

```sh
python tools/unsafe_fuzz.py fuzz --count 60 --seed 1
```

Run from a development checkout. The full guide supplies unsafe and composition commands.

The suite checks proofs against small DuckDB databases and replays counterexamples. It distinguishes false proofs, counterexamples that fail to show a difference, bugs in generated labels, and behavior-changing rule steps.

Finding no difference on the generated databases is executed evidence. It does not establish unbounded equivalence. Coverage and wrong-answer counts are separate so an unknown answer is not mistaken for a false proof.

## Tautologies and duplicates nobody reads

Two small simplifications let the prover finish cases it used to leave unknown.

- A test that can only be true or false, such as `(a IS NOT NULL) IS NULL`, is never NULL, so asking whether it is NULL always answers "no". `NOT NOT p` is just `p`, and a leftover `WHERE TRUE` filters nothing. Removing them makes two queries that differ only by such noise look identical. Example: `WHERE NOT NOT (a IS NOT NULL) OR (a IS NOT NULL) IS NULL` is the same filter as `WHERE a IS NOT NULL`. A comparison such as `(a = 1) IS NULL` is left alone, because a comparison can be NULL.
- `x IN (subquery)` only asks which values the subquery contains, not how often. So a `UNION DISTINCT` (duplicate removal) inside it can be read as `UNION ALL`. This stops as soon as something counts or picks rows: a `LIMIT`, an aggregate, a window or a join.

Limits of the evidence: the rules are checked by tests with near misses that must not be proved, and by a random run on small DuckDB databases. That shows no difference on those databases, not a proof for every query shape. See the [full reference](../../docs/evals/fuzzing.md) for the exact conditions.

