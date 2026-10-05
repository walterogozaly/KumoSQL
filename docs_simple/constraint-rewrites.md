# Changes that depend on data guarantees

[All simple guides](README.md) · [Full reference](../docs/constraint-rewrites.md)

Some SQL changes are correct only when certain facts about the data hold. KumoSQL records these facts as proof assumptions.

| Guarantee | Plain meaning |
| --- | --- |
| NOT NULL | This column never contains NULL |
| Unique key | No two rows share the same key under the declared constraint |
| Foreign key | A child's key refers to a matching parent key |

## Example: removing a join

```sql
SELECT o.customer_id
FROM orders AS o
JOIN customers AS c ON o.customer_id = c.id
```

You might want to read only `orders`. That is valid under the relevant declared guarantees: every order has a non-NULL customer ID, it refers to a customer that exists, and `customers.id` is unique.

Without a matching customer, the join drops an order. With two matching customers, it duplicates the order. A nullable customer ID can also be dropped by the join. The simplification requires the `ON` clause to contain only the key equality; extra conditions keep the join. If an earlier outer join may have padded the child row with NULLs, the declared NOT NULL fact does not apply to those rows. A `WHERE child.customer_id IS NOT NULL` filter can remove them before the join is simplified.

## Find which facts the proof needs

The Python helper `kumosql.constraint_dependence.needed_guarantees` starts with the declared facts, removes them one at a time (repeating until nothing more can go), and checks which ones are needed for a sufficient proof. The proof it keeps is the one for the final list, not the one for the full list. Its output can say “customer_id is NOT NULL” or “id is unique in customers.”

The result describes the assumptions supporting this proof method. It does not discover or enforce those guarantees in live data, or establish the only possible set of assumptions. A fact it keeps is one this prover could not do without, not a fact proven necessary.

The catalog and Dataform assertions can supply declarations. Verify that your real data satisfies them before relying on a conditional proof.

The evaluation includes legal examples and deliberately broken guarantees. It checks that a missing guarantee blocks the proof and that a counterexample actually changes the results. Details and rerun instructions are in the full reference.
