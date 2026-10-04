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

## Comments

A comment such as `-- note` does not change what a query returns, so it does not stop two queries from being proven the same. The one exception is a comment holding a Dataform `${...}` expression: Dataform fills those in even inside comments, and the result can turn into real SQL, so such a comment is compared like code. For example `SELECT 1 AS a -- note` and `SELECT 1 AS a` are proven the same. See the [full reference](../docs/provers.md) for details.

## What if the answer is unknown?

It may mean the SQL uses an unsupported feature, a table's columns are missing, or the solver reached its time or work limit. It does not establish that the queries differ. Text that cannot be read at all (an unclosed quote, very deep nesting) and queries that would take too long to even set up (a chain of CTEs that each read the previous one twice) also come back unknown rather than as an error.

Each solver check has a fixed work limit as well as a time limit. When the work limit stops a check, the same query pair gets the same answer on a fast or a busy machine; the result says "timed out" only when the clock stopped it, which a faster machine might not. With a very small time limit the clock usually stops a check first, so whether a pair is proven or unknown can still vary with machine load; it never flips between proven and different. The bounded check follows the same work limit, and a number such as `1e100000000` is declined everywhere rather than read. `GROUP BY ALL` is read as the columns it actually groups by, so an aggregate-only query still returns its one row on an empty table.

`SELECT * EXCEPT (b)`, `* REPLACE (...)` and `* RENAME (...)` change which columns a query returns, so the provers either apply them or say unknown. Example: `SELECT * EXCEPT (b) FROM t` is no longer treated as the same query as `SELECT * FROM t`. The cases the SMT prover cannot apply (a name the star does not have, `* ILIKE`) come back unknown, and the bounded checker always declines a modified star. See the full reference for details.

A replayed counterexample does establish a difference: the report includes a database where the results disagree. Matching a few random databases does not establish that no counterexample exists. That agreement is also weaker than BigQuery agreement: the queries run on DuckDB, decimals are compared to 12 significant digits, and column types are not compared.

Two details of the structural check: it drops a result ordering only when no sort key could raise an error (a `LIMIT` that cannot cut any row does not change that), and it treats a function that might be a user-defined one as a different function when its spelling differs, because BigQuery reads those names case-sensitively.

## Comparing scripts that change a table

An `UPDATE` or `DELETE` only reports how many rows it touched, and two updates can touch the same number of rows while writing different values: `UPDATE t SET a = 1 WHERE TRUE` and `UPDATE t SET a = 2 WHERE TRUE` both touch every row. The executed comparison therefore works on a private copy of the table and compares the rows that are left afterwards, not the count. This also covers `TRUNCATE`.

Some writes cannot be compared faithfully, so the check answers `error` instead of guessing: `MERGE` and `UPDATE ... FROM` (BigQuery fails when a target row matches several source rows, while DuckDB quietly picks one), an `UPDATE` or `DELETE` with no `WHERE` clause (BigQuery rejects it), and a statement whose target table cannot be identified. The limit is that a script writing several tables is still compared on the last table it wrote. See the [full reference](../docs/provers.md) for details.

Repeating the same comparison should find the same counterexample regardless of earlier comparisons or unrelated imports. For example, removing a lookup join can lose its treatment of a user whose plan is NULL: the join drops that user, while reading the users table keeps them. The solver isolates its candidate search to avoid changing this witness with process history. The search can still miss a difference or run out of time; returned examples must respect the declared data guarantees and make the query results differ.

## Strings compared with numbers

`WHERE '2' <> 2` looks like a condition that is never true, but engines disagree: MySQL turns the string into a number and finds them equal, DuckDB and PostgreSQL cast it the same way, and BigQuery refuses the query. Treating the two as always different once led the checker to say this query matches one with no filter, when real engines return different rows.

The checkers no longer guess: they treat the result of a string-versus-number comparison as unknown, so that query is "not proven" equal to the one without the filter, while two queries that make the same such comparison in the same way can still match. A few other forms (for example `IN` lists, or one column compared with both a string and a number) are simply declined. Comparing two numbers or two strings is unaffected. The limit is that only comparisons the checker can see are caught: a string reaching a number through a join or a `COALESCE` is not covered. See the [full reference](../docs/provers.md) for the exact list.

When results are compared by running both queries, a value keeps its kind: `TRUE` is not `1`, a NaN is not the text `NaN`, and a struct is not a list of pairs. Floats are compared rounded to 12 significant digits unless you ask for an exact comparison, and each result records which one it used.

## A limit that moves into a projection

KumoSQL knows that `SELECT d.b + 1 FROM (SELECT a AS b FROM t ORDER BY a LIMIT 1) AS d` returns the same row as `SELECT a + 1 FROM t ORDER BY a LIMIT 1`: it takes the first row, then computes the value. That rewrite had three holes, all now closed. When the outer query contained its own subquery, such as `(SELECT MAX(b) FROM u)`, renaming `d.b` to `a` also changed which column that subquery read, so two different queries were called equal. A value like `RAND()` that the outer query used twice would be drawn twice after the rewrite. And a `GROUP BY 1` could point at a different item once the select list changed. In each case the checker now declines, so the pair is "not proven" rather than wrongly "proven", and the common rewrites still go through. The evidence is a handful of hand-made witnesses plus fuzzing, not a proof that no such hole is left. The [full reference](../docs/provers.md) has the details.

Proofs may depend on declared keys, non-NULL columns, arithmetic assumptions, or restrictions on runtime errors. Check those before applying a change to real data. [Constraint-dependent rewrites](constraint-rewrites.md) explains data guarantees, and [bounded verification](evals/bounded-verification.md) explains the row limit.

## Example: a DISTINCT that can move outward

A query that removes duplicates inside a subquery, then joins it to a table on whole-number key columns, can have its duplicate removal moved to the outside when the join already makes every output row unique. KumoSQL's prover applies that move only when the columns are declared whole numbers and the joined tables' keys are fully pinned down; any grouping, limit, outer join or other twist makes it decline and leave the pair unproven. The evidence is a handful of textbook query pairs, so treat it as a narrow rule. The reference page has the exact conditions and the recorded scores: [Full reference](../docs/provers.md).
