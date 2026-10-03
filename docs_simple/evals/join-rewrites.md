# When changing a join type is safe

[Simple eval index](README.md) · [Full reference](../../docs/evals/join-rewrites.md)

This suite compares hand-checked query pairs involving CROSS, INNER, RIGHT, FULL, LEFT, semi, and anti joins.

The key question is often what happens to unmatched rows and duplicate matches.

## An example

```sql
SELECT a.id
FROM a LEFT JOIN b ON a.k = b.k
WHERE b.k IS NOT NULL
```

The WHERE clause removes the unmatched rows introduced by the LEFT JOIN. For this equality-join shape, an INNER JOIN can produce the same rows.

Removing that WHERE clause changes the question: unmatched left rows survive. RIGHT JOIN can become LEFT JOIN by swapping its sides, but the selected columns still need to line up.

## Traps the tests include

An anti join finds rows with no match. Testing a nullable right-hand value can misidentify a matched row as missing. `NOT IN` also behaves differently from `NOT EXISTS` when NULLs are possible.

A semi join only asks whether a match exists; an ordinary join can multiply a left row when several right rows match.

The suite includes equivalent pairs, non-equivalent pairs with counterexamples, declared data guarantees, and held-out cases. A failed proof remains unknown; it is not a refutation. The full guide describes each supported shape and the recorded results.
