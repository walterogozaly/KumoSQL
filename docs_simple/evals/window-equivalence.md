# Window functions and ties

[Simple eval index](README.md) · [Full reference](../../docs/evals/window-equivalence.md)

A window function such as `ROW_NUMBER() OVER (PARTITION BY user_id ORDER BY ts DESC)` numbers or compares rows inside a group. If two rows have the same `ts`, BigQuery does not say which comes first, so "the latest row per user" can return a different row each time. Two queries that look alike can then differ: `ROW_NUMBER() = 1` keeps one of the tied rows, `RANK() = 1` keeps all of them.

This suite is a set of query pairs built around that trap, plus its harmless look-alikes. Some pairs are the same query written two ways (a tool should prove that). Some differ only when two rows tie (a tool must never prove those equal). Some look risky but are not: if you only read the `ts` column afterwards, it does not matter which tied row is first.

## A concrete example

Both queries below return the latest event per user.

```sql
SELECT user_id, value FROM events
QUALIFY ROW_NUMBER() OVER (PARTITION BY user_id ORDER BY ts DESC) = 1

SELECT user_id, value FROM events
QUALIFY RANK() OVER (PARTITION BY user_id ORDER BY ts DESC) = 1
```

With distinct `ts` values they agree. With two events at the same latest `ts` the first keeps one of them and the second keeps both, so they are not equivalent. If you add the unique `id` to the order (`ORDER BY ts DESC, id DESC`), no two rows tie and the two queries become equivalent.

## Where the pairs come from

- Window pairs the workstream started with, written again here (15).
- BigQuery idioms written for this suite: latest row per key, sessions, running totals, gaps and islands (62). Each says why it holds.
- Window queries from the DuckDB test suite, each with pairs made by KumoSQL's own rewrites, by wrapping the query, and by changing one clause of the window (81). The SQLite test suite has no window queries.
- Window pairs from other benchmarks' development samples (92). Those come without labels, so they count for coverage and for wrong answers, not for the share of equivalent pairs proved.

A quarter of the cases is held out by a fixed hash of the case name, so adding cases never reshuffles it.

## What is scored

How many equivalent pairs are proved, how many non-equivalent pairs are shown different by a database that really separates them, and whether any pair was proved that is not equivalent or called different when it is equivalent. "Unknown" is an allowed answer. The first run was taken before any window rule was added, so it shows where the prover starts; later rule changes move the numbers.

The labels are checked by running both queries on DuckDB over every row order of small tie-heavy databases and comparing the sets of results. A non-equivalent case carries a database where the two sets differ.

## What to keep in mind

- The pairs were written by the person who also reads the prover, so this is a safety net, not an independent test.
- The labels rest on small databases run on DuckDB; two cases that DuckDB cannot run (`ARRAY_AGG` with `LIMIT`) are labelled by reasoning.
- Pairs made by KumoSQL's rewrites are called equivalent because the rules are sound and the two queries agree on the test data, which is evidence, not a proof.

See the full reference for the recorded scores and the list of sources.
