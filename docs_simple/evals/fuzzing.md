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

## Differences that only show on unusual values

The generated test databases only use the numbers 0 to 3, so a change that matters only for a negative number, or for a number bigger than any in the query, can look harmless although it is not. When the usual searches find nothing, the checker now also tries two extra kinds of small databases: ones where a boundary row sits alone in its table, and ones that the solver builds on purpose so that the two queries disagree (a few rows, small values only).

For example, `WHERE a >= 3` against `WHERE a > 3` differs on a single row with `a = 3`, as long as no matching row exists in the second table. A database like that is only a suggestion: it is accepted as a difference only after both queries really run on it and return different rows, also with the database engine's optimizer turned off, so a bug in the solver's model can lose a difference but never invent one. Two details of how the engines read SQL differently are pinned down for this: rounding a decimal to an integer, and dates beyond year 9999.

Limits of the evidence: finding no such database proves nothing, and the searches only cover queries made of constructs whose results are known to match between the two engines. See the [full reference](../../docs/evals/fuzzing.md#refutations-that-need-exact-values-bounded-refutation) for the details.
