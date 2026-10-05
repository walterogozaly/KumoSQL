# Queries whose answer depends on ties

[All simple guides](README.md) · [Full reference](../docs/ties.md)

Sometimes a query keeps "the latest row per user", but two rows share the latest timestamp. The database may return either one, and the answer can change from one run to the next. KumoSQL can point at these places before they cause a surprise.

For example:

```sql
SELECT user_id, ts, value FROM events
QUALIFY ROW_NUMBER() OVER (PARTITION BY user_id ORDER BY ts DESC) = 1
```

If `events` has a unique `id`, adding `id` to the `ORDER BY` makes the choice the same every time. KumoSQL reports exactly that suggestion. If the result only uses `user_id` and `ts`, the query is already safe: either tied row gives the same output.

```python
from kumosql.tie_determinism import analyze

report = analyze("SELECT a FROM t ORDER BY b LIMIT 1")
for site in report.sites:
    print(site.kind, site.verdict, site.fix)
```

Each place that can depend on ties (a window function, `LIMIT`, `ANY_VALUE`, `ARRAY_AGG` and similar) is called `deterministic` or `unknown`. `unknown` means no reason was found that the result is stable. It does not prove the result changes.

## Showing the surprise: tie witnesses

`unknown` is a warning, not proof. `kumosql.tie_witness.find_tie_witness` goes further: it builds a tiny database (usually two rows that tie) and two ways of storing those rows, and shows the query returning different answers for the two. In the example above that is two events of one user with the same timestamp but different values: store them one way and you get one value, store them the other way and you get the other.

```python
from kumosql.tie_witness import find_tie_witness, replay

witness = find_tie_witness(sql, schema, rules)   # a small JSON document, or None
replay(witness)                                   # True when it still shows the difference
```

The database always obeys the keys and NOT NULL columns you declared, so the surprise is not the result of impossible data. Both stored orders are run again, with and without DuckDB's optimizer, before the witness is returned.

Limits: finding nothing does not mean the query is safe, because only a few dozen small databases are tried. A witness is about the whole query, not one window inside it.

## What the check does and does not show

- It reads the query text and any declared keys; it runs no query and uses no data.
- The checks in the repository run the queries judged deterministic on DuckDB with the table rows stored in every order, and each returns the same rows. That is a test of the rule on small tables, not a guarantee for every BigQuery query.
- It adds no score to the benchmark scoreboard.
- The witness search runs only on DuckDB; BigQuery itself may pick yet another row.

The full guide lists every kind of place and the reasons it can be called deterministic: [Ties and nondeterministic results](../docs/ties.md).
