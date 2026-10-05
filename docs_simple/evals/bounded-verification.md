# Checking every small database

[Simple eval index](README.md) · [Full reference](../../docs/evals/bounded-verification.md)

Bounded verification asks: do these queries agree on every database with at most N rows per table, within the modeled SQL and assumptions?

At a bound of 3, each table can have zero, one, two, or three rows. Z3 reasons about symbolic cell values and NULLs, rather than trying only a few randomly chosen values.

Columns keep to the values they can really hold: a `DATE` stays between year 1 and year 9999 and a `NUMERIC(10, 2)` keeps two decimals, so a counterexample never needs a date or number the database could not store. See the [full reference](../../docs/evals/bounded-verification.md) for the details.

## Read the result

- **Bounded, 3 rows** means no difference exists within that model and row limit. It says nothing about larger databases.
- **Counterexample** means the solver found data and execution confirmed different results.
- **Unknown** means unsupported SQL, a time limit, or an unconfirmed model prevented an answer.

Bounded evidence is separate from an unbounded equivalence proof. It is also stronger within its row limit than random execution of just a few databases.

## In the app

**Settings → Solver** controls the row bound; zero turns it off. Compare queries and Compare tables display the bounded result alongside other evidence. Saved schemas and constraints are needed to describe the source tables.

The checker replays candidate counterexamples in DuckDB and reruns both queries under the ways a tie could be broken (rows stored in other orders, a `LIMIT` cut at another tied row, `ANY_VALUE` picks) so an arbitrary tie choice is not treated as a real difference; see [refuting on one database](../../docs/ties.md#refuting-on-one-database). Its arithmetic and runtime-error assumptions still matter.

For SQLite, only replayed counterexamples are offered: differences between the symbolic model and SQLite mean a successful bounded search cannot be reported as bounded equality there. The full guide covers encoding tests, assumptions, rerun commands, and scores.
