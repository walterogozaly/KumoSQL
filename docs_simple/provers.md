# How KumoSQL checks whether queries match

[All simple guides](README.md) · [Full reference](../docs/provers.md)

Two queries are equivalent when they return the same results under the comparison's rules. Usually that means the same rows with the same duplicate counts, ignoring unspecified row order. Column names, types, ordering, and declared data guarantees can also matter; read the check's assumptions.

Suppose you replace `WHERE 1 = 1` with no WHERE clause. A checker can establish that the removed condition never filtered anything. More complicated changes need stronger reasoning.

## The different kinds of checks

| Check | What a successful result establishes |
| --- | --- |
| Structural proof | After safe simplifications, both queries have the same supported structure |
| SMT / algebraic proof | Mathematical reasoning establishes equivalence for the supported SQL under listed assumptions |
| Bounded verification | No difference exists in the model for any database up to a stated row limit |
| Executed comparison | Both queries matched on the particular datasets tested |
| BigQuery dry run | The statements plan and, for a rewrite check, their output schemas match |

The first two can give an unbounded proof: the database's row count has no fixed test limit. The bounded check covers every modeled value combination, including NULLs, within its row bound. Random-data execution only covers the databases actually tried. A dry run does not compare returned values.

## Which prover is which?

The structural prover compares normalized query trees. The SMT prover uses Z3, a solver that reasons about constraints. The algebraic prover treats duplicate row counts as arithmetic, helping it reason about joins, unions, and aggregates. The optional Java SQLSolver backend is a separate setup; ordinary Python proofs do not require Java.

From a checkout, install solver and execution support with:

```sh
python -m pip install ".[smt,execution]"
```

A small Python example:

```python
from kumosql import prove_equivalent

result = prove_equivalent(
    "SELECT id FROM orders WHERE 1 = 1",
    "SELECT id FROM orders",
)
print(result.status.value)  # proven_equivalent
```

## What if the answer is unknown?

It may mean the SQL uses an unsupported feature, a table's columns are missing, or the solver reached its time or work limit. It does not establish that the queries differ. Text that cannot be read at all (an unclosed quote, very deep nesting) and queries that would take too long to even set up (a chain of CTEs that each read the previous one twice) also come back unknown rather than as an error.

Each solver check has a fixed work limit as well as a time limit. When the work limit stops a check, the same query pair gets the same answer on a fast or a busy machine; the result says "timed out" only when the clock stopped it, which a faster machine might not. `GROUP BY ALL` is read as the columns it actually groups by, so an aggregate-only query still returns its one row on an empty table.

A replayed counterexample does establish a difference: the report includes a database where the results disagree. Matching a few random databases does not establish that no counterexample exists.

Proofs may depend on declared keys, non-NULL columns, arithmetic assumptions, or restrictions on runtime errors. Check those before applying a change to real data. [Constraint-dependent rewrites](constraint-rewrites.md) explains data guarantees, and [bounded verification](evals/bounded-verification.md) explains the row limit.

## Example: a DISTINCT that can move outward

A query that removes duplicates inside a subquery, then joins it to a table on whole-number key columns, can have its duplicate removal moved to the outside when the join already makes every output row unique. KumoSQL's prover applies that move only when the columns are declared whole numbers and the joined tables' keys are fully pinned down; any grouping, limit, outer join or other twist makes it decline and leave the pair unproven. The evidence is a handful of textbook query pairs, so treat it as a narrow rule. The reference page has the exact conditions and the recorded scores: [Full reference](../docs/provers.md).
