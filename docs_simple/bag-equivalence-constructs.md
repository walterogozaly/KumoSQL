# More SQL the bag-equivalence backend can compare

[All simple guides](README.md) · [Full reference](../docs/bag-equivalence-constructs.md)

The bag-equivalence backend is one of KumoSQL's provers. It treats a query as a count of how many times each row appears and asks Z3 whether two queries always give the same counts. This page lists the less common SQL it understands, with an example of each.

Suppose one query writes `GROUP BY ROLLUP(a)` and another writes the group by `a` and the grand total as a `UNION ALL`. The backend rewrites the first into the second form and compares them. In the same way it reads:

- `COUNT(*) FILTER (WHERE b > 1)` as a count over only the rows where `b > 1`.
- `COUNT(DISTINCT a, b)` as the number of different pairs where neither value is NULL.
- `FETCH FIRST 3 ROWS ONLY` as `LIMIT 3`.
- `JOIN LATERAL`, where the right side may read the columns to its left.
- A parenthesized join inside another join.
- A comparison of a number with a string, as "some fixed answer for each pair of values" (it does not guess what the engine does).

Some constructs only work when both queries spell a piece the same way. A subquery with `LIMIT` and a select with a window function are treated as black boxes: they are equal if their text is equal (table aliases may differ). The proof then says it assumes the engine returns the same rows for the same text. A top-level `LIMIT` without `ORDER BY` is never proved, because the engine may pick any rows.

## Limits of the evidence

A "proved" answer is only as good as its listed assumptions. "Not proven" does not mean the queries differ: the backend never says two queries are different. The tests in `tests/test_uexpr_constructs.py` and the benchmark runner check every proof against random data, which catches a wrong translation but cannot show one is right. The recorded scores are in the [SQLSolver evaluation page](evals/sqlsolver.md).
