# How KumoSQL checks whether queries match

[All simple guides](README.md) · [Full reference](../docs/provers.md)

None of these checks uses the network. Calling a prover or a rewrite from Python makes no request; the one optional lookup (the columns of tables the project does not define) is off unless you turn it on. See [pipeline analysis](pipeline-analysis.md).

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

A replayed counterexample does establish a difference: the report includes a database where the results disagree. Matching a few random databases does not establish that no counterexample exists. That agreement is also weaker than BigQuery agreement: the queries run on DuckDB, decimals are compared to 12 significant digits, and column types are not compared.

Two details of the structural check: it drops a result ordering only when no sort key could raise an error (a `LIMIT` that cannot cut any row does not change that), and it treats a function that might be a user-defined one as a different function when its spelling differs, because BigQuery reads those names case-sensitively.

Repeating the same comparison should find the same counterexample regardless of earlier comparisons or unrelated imports. For example, removing a lookup join can lose its treatment of a user whose plan is NULL: the join drops that user, while reading the users table keeps them. The solver isolates its candidate search to avoid changing this witness with process history. The search can still miss a difference or run out of time; returned examples must respect the declared data guarantees and make the query results differ.

## Strings compared with numbers

`WHERE '2' <> 2` looks like a condition that is never true, but engines disagree: MySQL turns the string into a number and finds them equal, DuckDB and PostgreSQL cast it the same way, and BigQuery refuses the query. Treating the two as always different once led the checker to say this query matches one with no filter, when real engines return different rows.

The checkers no longer guess: they treat the result of a string-versus-number comparison as unknown, so that query is "not proven" equal to the one without the filter, while two queries that make the same such comparison in the same way can still match. A few other forms (for example `IN` lists, or one column compared with both a string and a number) are simply declined. Comparing two numbers or two strings is unaffected. The limit is that only comparisons the checker can see are caught: a string reaching a number through a join or a `COALESCE` is not covered. See the [full reference](../docs/provers.md) for the exact list.

Proofs may depend on declared keys, non-NULL columns, arithmetic assumptions, or restrictions on runtime errors. Check those before applying a change to real data. [Constraint-dependent rewrites](constraint-rewrites.md) explains data guarantees, and [bounded verification](evals/bounded-verification.md) explains the row limit.

## Example: whole numbers that turn into decimals

A database that compares a whole number with a decimal column first converts the whole number to a decimal, and a very large whole number loses its last digits in that conversion. So `a = b AND b = c` does not always mean `a = c`: 9007199254740992 and 9007199254740993 both equal the decimal 9007199254740992.0. Likewise `1e-324 < 2e-324` is false, because both literals round to zero. KumoSQL's SMT prover now models the conversion when the column types are declared, and it does not treat tiny, huge or long decimal literals as exact numbers. When column types are not declared, a proof that compares columns states the assumption that they have the same type. The evidence is regression pairs; the exact rules are in the [full reference](../docs/provers.md).

## Example: a DISTINCT that can move outward

A query that removes duplicates inside a subquery, then joins it to a table on whole-number key columns, can have its duplicate removal moved to the outside when the join already makes every output row unique. KumoSQL's prover applies that move only when the columns are declared whole numbers and the joined tables' keys are fully pinned down; any grouping, limit, outer join or other twist makes it decline and leave the pair unproven. The evidence is a handful of textbook query pairs, so treat it as a narrow rule. The reference page has the exact conditions and the recorded scores: [Full reference](../docs/provers.md).
