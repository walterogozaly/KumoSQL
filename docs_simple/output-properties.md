# Facts a query guarantees about its output

[All simple guides](README.md) · [Full reference](../docs/output-properties.md)

KumoSQL can infer some facts without running SQL: whether a column can be NULL, which columns identify a row, and whether a query can return more than one row.

For example:

```sql
SELECT customer_id, COUNT(*) AS orders
FROM orders
GROUP BY customer_id
```

There is one row per customer group, so `customer_id` identifies a group in the output. `COUNT(*)` is never NULL. That does not establish that `customer_id` itself is never NULL: the input may contain a NULL group.

## Try it in Python

```python
from kumosql.output_properties import infer_properties

props = infer_properties(
    "SELECT customer_id, COUNT(*) AS n FROM orders GROUP BY customer_id",
    constraints={},
    schema={"orders": ["customer_id"]},
)
print(props.non_null("n"))       # True
print(props.is_unique("customer_id"))  # True
```

Supply real schema and constraints for facts that depend on source tables.

## Common surprises

- `SUM(amount)` can be NULL when every value in a group is NULL.
- A LEFT JOIN can introduce NULLs even when the original right-hand column is declared NOT NULL.
- Joining to several matching rows can destroy a source table's uniqueness.
- A global `COUNT(*)` without GROUP BY returns one row even when its input is empty.

Uniqueness here uses the way GROUP BY and DISTINCT compare rows, treating NULLs as equal for duplicate detection.

Facts based on declared keys or NOT NULL columns carry assumptions. Facts derived entirely from the query need none. A fact not reported is not known; it is not automatically false.

Unsupported query shapes are reported explicitly. The [output-properties evaluation in the full guide](../docs/output-properties.md#evaluation) checks asserted facts against executed data. See [constraint-dependent rewrites](constraint-rewrites.md) for how source guarantees help proofs.
