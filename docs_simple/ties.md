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

Names can be confusing: in `SELECT value AS ts FROM events ORDER BY ts`, `ts` might mean the new name or the table's own `ts` column. When the answer matters and the query does not say, KumoSQL reports `unknown` instead of guessing.

## How the check is itself checked

A tool, `tools/tie_fuzz.py`, writes thousands of random queries with windows, `LIMIT`, `ANY_VALUE` and similar, asks KumoSQL which ones are safe, then runs each "safe" query on tiny tables with the rows stored in every possible order. If any "safe" query ever returns different rows, the safety check was wrong and the tool prints it. On 7,500 queries none did; adding queries where a `SELECT` rename reuses a column's name found real mistakes (13 in 1,000 queries), which were fixed. Full numbers and limits are in the [reference](../docs/ties.md#fuzzing-the-verdicts).

Limits: it only tries small tables of whole numbers, and it uses DuckDB's way of ordering ties, which is not BigQuery's. A clean run does not prove every query is safe.

## What the check does and does not show

- It reads the query text and any declared keys; it runs no query and uses no data.
- The checks in the repository run the queries judged deterministic on DuckDB with the table rows stored in every order, and each returns the same rows. That is a test of the rule on small tables, not a guarantee for every BigQuery query.
- It adds no score to the benchmark scoreboard.

The full guide lists every kind of place and the reasons it can be called deterministic: [Ties and nondeterministic results](../docs/ties.md).
