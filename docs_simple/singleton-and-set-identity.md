# Singleton aggregation and set identity, in plain language

[All simple guides](README.md) · [Full reference](../docs/singleton-and-set-identity.md)

Two kinds of query pairs are hard for the prover even though they match: a join that can only ever produce zero or one row, and a stack of UNIONs that differs only in how the inner parts are named.

## Example

A table is pinned to one key value, say `id = 1`, and joined to a grouped count on that same key. The join has at most one row, so summing the grouped count gives the same rows as using the count directly. KumoSQL's prover recognizes that shape and rewrites it so the two spellings read alike. For text keys it adds a condition to the result: the join must compare text the same way the grouping does (the same collation). The result lists that condition, and the person applying the change has to check it against the real tables.

The second bridge handles a UNION ALL that contains a UNION DISTINCT inside it. It wraps the whole tree so the two sides are compared with their parts' names ignored, without moving, merging, or reordering any branch.

## Limits

- Both bridges decline partial keys, OR conditions, outer joins, LIMIT, DISTINCT counts, and anything involving random or time-dependent values.
- A rewrite that needs a condition only runs when the caller collects conditions; otherwise it is refused.
- The rules were written while the benchmark cases they help were visible, so the benchmark gains are not evidence that they generalize. Finite random-data checks agreed on those pairs, but they are not a proof.

The full reference has the exact conditions and the recorded scores: [Full reference](../docs/singleton-and-set-identity.md).
