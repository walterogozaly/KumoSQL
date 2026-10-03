# Checking the VeriEQL benchmark pairs

[Simple eval index](README.md) · [Full reference](../../docs/evals/verieql.md)

This evaluation reads VeriEQL's benchmark query pairs, schemas, and constraints. KumoSQL uses its own deterministic prover and database generator; it does not call a language model or reuse VeriEQL's implementation.

The corpora include LeetCode solutions, pairs from equivalence research, and Calcite optimizer tests. They contain both equivalent and different queries, without gold labels in the input files.

## Read the verdict by its evidence

- **Equivalent** is an unbounded proof under the stated supported constraints and assumptions. Additional random databases cross-check it.
- **Different** includes a database satisfying the applicable constraints on which the queries return different results.
- **Unknown** means neither answer was established.

Candidate counterexamples are replayed and shuffled to avoid differences caused only by arbitrary row order or tied LIMIT choices. Some engine-sensitive cases receive additional checks with the optimizer disabled.

## Constraints matter

The input can declare keys, non-NULL columns, and other restrictions. A database violating those restrictions is not a valid counterexample to a conditional claim. Read which constraints the prover models and which the execution generator enforces.

The full guide documents translation, constraint handling, known non-equivalent pairs, commands, and separate proof/executed score rows. [Bounded verification](bounded-verification.md) covers KumoSQL's separate row-limited checker, inspired by the bounded approach used in VeriEQL's research.
