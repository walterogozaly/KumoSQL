# Checking the LLM-SQL-Solver query pairs

[Simple eval index](README.md) · [Full reference](../../docs/evals/llm-sql-solver.md)

This suite uses query pairs from the LLM-SQL-Solver project. Despite the name, KumoSQL decides these cases with deterministic proof and execution checks, without calling a language model.

There is a negative set that must never be proved equivalent and a relaxed set with expert labels.

## How it decides

The algebraic prover first tries to establish equivalence in SQLite semantics. Otherwise, generated and targeted databases can find a replayed difference. Unresolved cases stay unknown.

Output names are ignored under the benchmark's comparison rules. Ordered results are checked when the task requires them. The checker avoids assuming incomplete Spider key declarations are unique: a first column of a composite key need not be unique by itself.

## Why an expert label can disagree

Two queries may express similar intent on realistic data yet differ on a valid database. For example, `COUNT(nullable_column)` does not count NULLs, while `COUNT(*)` does. Different column order or sorting can matter too.

A replayable counterexample supports a semantic difference even when the published label says equivalent. Label agreement and correctness therefore need separate discussion.

The full guide documents disputed pairs, unsupported SQLite forms, and scores. Read the negative-set guard and the relaxed-label agreement separately; one percentage cannot explain both.
