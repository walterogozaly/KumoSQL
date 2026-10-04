# Generating tests to find unsafe changes

[Simple eval index](README.md) · [Full reference](../../docs/evals/fuzzing.md)

Fuzzing creates many queries and data shapes to look for mistakes. Here the generator is seeded: using the same seed reproduces the same cases.

## Three kinds of tests

- **Metamorphic tests** compare different ways to compute the same result. For example, splitting rows into “condition true,” “condition false,” and “condition NULL,” then recombining them should recover all rows.
- **Unsafe variants** make tempting changes such as dropping a duplicate-producing join or changing a NULL-sensitive filter.
- **Composition tests** apply several rewrite rules in sequence and check that no step changes results.

A fourth, typed soundness fuzzer tests proof claims on schemas with types and integrity constraints. Its found false proofs are soundness bugs; read the full guide's current findings before relying on the affected shapes.

NULLs need the third partition: in SQL, a condition can be unknown as well as true or false.

## What the unproven cases taught

After the first rounds, about ninety generated pairs were still unproven. Working through them added a handful of narrow rules: a query cut into pieces by a condition and recombined with `UNION DISTINCT` is read as the original query, a few BigQuery shapes (struct fields, `UNNEST` over a short list) are read as plain columns and `UNION ALL`, some always-empty queries are recognised, and a sum computed per group and multiplied back is read as the sum over the join. Each rule comes with near misses it must refuse: for example, the same pieces joined with `UNION ALL` keep their duplicates and are not the original query.

Some pairs turned out to really differ because the random databases had only drawn small values. The prover now searches for a database that separates them and reports a difference only when both queries actually run and disagree there. The first rounds could therefore not count those pairs as equivalent. The sample finds no false proofs, but that is evidence on generated cases, not a guarantee. Recorded scores are in the [full reference](../../docs/evals/fuzzing.md#frontier-rules); the development cases were used to write the rules, so the held-out families are the fairer measure.

## Run a sample

```sh
python tools/unsafe_fuzz.py fuzz --count 60 --seed 1
```

Run from a development checkout. The full guide supplies unsafe and composition commands.

The suite checks proofs against small DuckDB databases and replays counterexamples. It distinguishes false proofs, counterexamples that fail to show a difference, bugs in generated labels, and behavior-changing rule steps.

Finding no difference on the generated databases is executed evidence. It does not establish unbounded equivalence. Coverage and wrong-answer counts are separate so an unknown answer is not mistaken for a false proof.
