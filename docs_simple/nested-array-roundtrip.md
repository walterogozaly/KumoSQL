# ARRAY and UNNEST round trips, in plain language

[All simple guides](README.md) · [Full reference](../docs/nested-array-roundtrip.md)

Two queries can build the same array in different ways. Taking an array apart with `UNNEST ... WITH OFFSET` and putting it back together with `ARRAY(SELECT ... ORDER BY offset)` gives the same array you started with. KumoSQL's prover reads that long form as the plain column.

## Example

`SELECT ARRAY(SELECT t FROM UNNEST(tags) AS t WITH OFFSET AS pos ORDER BY pos) FROM orders` returns the same arrays as `SELECT tags FROM orders`, when `tags` is a column stored in the table. The prover proves the pair and lists one condition: a stored array is never NULL.

The same idea reads `FROM UNNEST([1, 2, 3]) AS x` as a short list of rows (`SELECT 1 UNION ALL SELECT 2 UNION ALL SELECT 3`), with `WITH OFFSET` counting from 0.

## Where it stops

- If the array can be NULL (a result of `ARRAY_CONCAT`, a column from the empty side of a `LEFT JOIN`), the long form returns an empty array and the short form returns NULL, so the two are different and KumoSQL does not fold them.
- A filter, `DISTINCT`, a different sort order, a limit or a computed element changes the array, so those are left alone.
- BigQuery does not say in which order `UNNEST` rows come back. When a query could depend on that order (a window, `LIMIT`, `ARRAY_AGG`, `STRING_AGG`), the literal rewrite is skipped.

The tests run these pairs on a local database, but the evidence is the reasoning above and the BigQuery facts it relies on; see the [full reference](../docs/nested-array-roundtrip.md) for the exact conditions.
