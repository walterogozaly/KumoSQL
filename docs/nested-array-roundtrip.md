# ARRAY and UNNEST round trips

[Plain-language version](../docs_simple/nested-array-roundtrip.md)

Three exact identities over arrays, applied by the algebraic prover's normalizer (`src/kumosql/nested_array_roundtrip.py`, one entry in `algebraic_equivalence.normalize`). They are part of the nested-data work (#505).

| Query shape | Read as | Condition |
| --- | --- | --- |
| `ARRAY(SELECT x FROM UNNEST(a) AS x WITH OFFSET o ORDER BY o)` | `a` | `a` cannot be NULL: an array literal, or a column the schema types `ARRAY<..>` of a table, read from that table's own row (not the null-extended side of an outer join) |
| `FROM UNNEST(ARRAY(SELECT x FROM UNNEST(a) AS x WITH OFFSET o ORDER BY o))` | `FROM UNNEST(a)` | any `a`, in `FROM` or a join (a NULL array and an empty one both unnest to no rows, and the offsets follow the same order) |
| `FROM UNNEST([1, 2]) AS x WITH OFFSET AS o` | `FROM (SELECT 1 AS x, 0 AS o UNION ALL SELECT 2, 1) AS x` | scalar literals of one kind (integers, decimals, strings or booleans, NULLs allowed), `FROM` or an inner or cross join, and no construct whose value can depend on row order |

## Why the conditions

- `ARRAY(subquery)` over a NULL array is `[]`, while the NULL array stays NULL (`UNNEST(NULL)` gives no rows). So a computed array (`ARRAY_CONCAT(a, b)` is NULL when an argument is NULL, a struct field, a view's column) is never folded. A stored array is never NULL: BigQuery stores a NULL array as empty. A table column's array on the null-extended side of a `LEFT`, `RIGHT` or `FULL` join is NULL for an unmatched row, so that is declined too. A proof that folds a stored column lists the assumption "a stored ARRAY column is never NULL".
- The round trip keeps order, length and NULL elements, so it needs no assumption about elements.
- Only the exact shape folds: one select item that is the element, the `UNNEST` as the only source, `ORDER BY` the offset ascending and nothing else. A filter, `DISTINCT`, `GROUP BY`, a second sort key, a descending order, `LIMIT`, `SELECT AS STRUCT`, a computed element or a missing `ORDER BY` leaves the query as written.
- `x IN UNNEST(<round trip>)` is not folded for a computed array: whether `NULL IN UNNEST(NULL)` equals `NULL IN UNNEST([])` is not relied on.
- UNNEST of literals is the same bag of rows as the UNION ALL, but BigQuery fixes the order of neither. The rewrite is skipped for a statement with a window, `LIMIT`/`OFFSET`, `ARRAY_AGG`, `STRING_AGG`, `ANY_VALUE`, `FIRST`/`LAST` or `RAND`, because a result that depends on UNNEST row order is not caught by the reversed-rows stability check. WITH OFFSET counts from 0.

`ARRAY_AGG(.. ORDER BY offset)` over an UNNEST, windows over UNNEST and unordered `ARRAY(SELECT ..)` are not rewritten.

## Tests

`tests/test_nested_array_roundtrip.py` covers each fold, a case for each condition that must not fold, the traps the prover must not prove (a repeated element against `UNION DISTINCT`, offsets from 1, a NULL-extended array, `ARRAY_CONCAT`, reversed or filtered subqueries), and runs the folded UNNEST queries on DuckDB through the BigQuery translation over data with empty arrays, NULL arrays and repeated elements.
