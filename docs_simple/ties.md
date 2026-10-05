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

## Refuting a difference on real rows

When KumoSQL says two queries are different, it shows a small database where they return different rows. If both queries return a tied row, the difference might only be "this one kept the first tied row and that one kept the second". That is not a different answer, so it must not count.

For example, "latest event per user by `ROW_NUMBER`" and "the smallest value among the latest events" return different rows on a database where two events of a user share the latest time and hold different values. But the first query is allowed to return the second one's row, so it is not a counterexample. Now KumoSQL runs each query again on that database with its rows stored in other orders, with each `LIMIT` cut at another tied row, and with `ANY_VALUE` made to fail when it could pick several values. It reports the difference only if both queries give one answer however the tie goes, or one does and the other never matches it. Otherwise the answer is "unknown" and the search tries other databases, so a pair that really differs is still refuted on a database without ties.

The limits: it tries a list of tie-breaks, all of them on tables of up to four rows and a sample on larger ones, so "no tie" is evidence, not proof. This only ever withholds a refutation, never adds a proof. Details: [Refuting on one database](../docs/ties.md#refuting-on-one-database).

## What the check does and does not show

- It reads the query text and any declared keys; it runs no query and uses no data.
- The checks in the repository run the queries judged deterministic on DuckDB with the table rows stored in every order, and each returns the same rows. That is a test of the rule on small tables, not a guarantee for every BigQuery query.
- It adds no score to the benchmark scoreboard.

The full guide lists every kind of place and the reasons it can be called deterministic: [Ties and nondeterministic results](../docs/ties.md).
