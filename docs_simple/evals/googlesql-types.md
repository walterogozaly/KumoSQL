# Checking the type checker against Google's tests

[Simple eval index](README.md) · [Full reference](../../docs/evals/googlesql-types.md)

KumoSQL can work out the type of every output column of a BigQuery query without running it (see [type inference](../type-inference.md)). This eval asks: when it names a type, is that the type Google's own reference gives?

Google publishes compliance tests for GoogleSQL, the language of BigQuery. Each test has a query and the result it must return, printed with types, such as `ARRAY<STRUCT<a INT64, b STRING>>`. The eval reads the column types off those printed results and compares them with what the checker says for the same query, using the tables each test file sets up.

## An example

A test selects `[1, 2.5]` and expects an `ARRAY<FLOAT64>`: the list holds an integer and a decimal literal, and GoogleSQL turns both into floating point. If the checker says `ARRAY<FLOAT64>`, the column is **exact**. If it says nothing, the column is **unknown**. If it says `ARRAY<INT64>`, the column is **wrong**, and that must never happen.

## How to read the score

- **Exact** is a hit. **Unknown** is allowed but is a miss: it means the checker held back. **Wrong** is a bug.
- The headline counts only columns whose types exist in BigQuery, since a type BigQuery lacks is of no use to a BigQuery user.
- Saying "unknown" every time would give zero wrong, so what matters is that the exact count rises while wrong stays zero.
- For comparison, the same cases are run through sqlglot's built-in type guesser. It gets less than half exactly right, is wrong in hundreds of columns and crashes on over a thousand queries; the full page has the figures.

## Limits of the evidence

- The tests are Google's, not yours, and they show agreement with Google's reference implementation, not with BigQuery in every case.
- The checker is developed against most of the test files, so that score is optimistic. A quarter of the files, chosen by a fixed rule on the file name, were held out and scored once, at the end. That score is clearly lower than the one on the files the checker was built against, which is the honest picture of how it does on queries it has not seen. In both, the checker never gave a wrong type. The numbers are on the full page.
- Even then, the held-out part comes from the same suite, so it measures new queries of the same kind, not new kinds of query.
- A second, separate check runs the checker over real BigQuery projects whose queries are known to run; there it must report no mistakes at all.

The full reference has the pinned commit, the split rule, the case counts, the current score and how to run it: `python tools/googlesql_types_eval.py`.
