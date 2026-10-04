# Checking the VeriEQL benchmark pairs

[Simple eval index](README.md) · [Full reference](../../docs/evals/verieql.md)

This evaluation reads VeriEQL's benchmark query pairs, schemas, and constraints. KumoSQL uses its own deterministic prover and database generator; it does not call a language model or reuse VeriEQL's implementation.

The corpora include LeetCode solutions, pairs from equivalence research, and Calcite optimizer tests. They contain both equivalent and different queries, without gold labels in the input files.

## Read the verdict by its evidence

- **Equivalent** is an unbounded proof under the stated supported constraints and assumptions. Additional random databases cross-check it.
- **Different** includes a database satisfying the applicable constraints on which the queries return different results.
- **Unknown** means neither answer was established.

Candidate counterexamples are replayed and shuffled to avoid differences caused only by arbitrary row order or tied LIMIT choices. Some engine-sensitive cases receive additional checks with the optimizer disabled.

## Why the test run is cheaper

Almost all of a run is DuckDB answering both queries on many tiny databases. The search now uses a single DuckDB thread, rewrites only the tables whose rows changed, and does not rerun a database it has already seen both queries agree on. The databases it tries are the same, so the answers are the same, with one known exception: when a query picks an arbitrary value from a group, DuckDB's pick can depend on how the table was filled, so one LeetCode pair whose refutation rested on a lucky pick is no longer refuted. Timings and the pair are in the [full reference](../../docs/evals/verieql.md); they come from one container and a sample of the pairs, not the whole corpus.

## Constraints matter

On Windows, each query-pair check runs in a separate process so the harness can stop it at its time limit. A timeout means unknown if the initial data search did not finish, or agreement on the tried data if it did; it never means proof. A crashed process also means unknown. Synthetic tests cover these outcomes and parallel operation. Starting a process takes part of the budget, so timings can differ between platforms. See the [full reference](../../docs/evals/verieql.md).

The input can declare keys, non-NULL columns, and other restrictions. A database violating those restrictions is not a valid counterexample to a conditional claim. Read which constraints the prover models and which the execution generator enforces.

The full guide documents translation, constraint handling, known non-equivalent pairs, commands, and separate proof/executed score rows. [Bounded verification](bounded-verification.md) covers KumoSQL's separate row-limited checker, inspired by the bounded approach used in VeriEQL's research.
