# Finding duplicate work

[Simple eval index](README.md) · [Full reference](../../docs/evals/duplicate-detection.md)

This suite generates projects with known copies of queries, similar queries, and deceptive near-matches. It tests whether KumoSQL can find shared work without confusing different results.

Renaming an alias or reordering `AND` conditions can leave a query equivalent. Changing `>` to `>=`, INNER JOIN to LEFT JOIN, or SUM to AVG can change its meaning even when the text is very similar.

## Separate the measurements

Exact-copy detection, near-copy detection, and proposed shared-model refactors are scored separately. A near-copy is a candidate for investigation; it is not automatically an interchangeable result.

For a proposed shared model, the suite checks affected consumers and records proof and execution evidence separately. Identical SELECT text over differently defined CTEs is a trap: the surrounding definitions matter.

## Run it

```sh
python tools/dup_bench.py
```

Use a development checkout with its test dependencies. The full reference provides larger project sizes, JSON output options, and performance checks.

**Precision** asks how many reported matches are correct. **Recall** asks how many real matches were found. Read both, along with readiness of the proposed refactors.

The examples come from a seeded generator, so they are repeatable but do not cover every real project shape. Development families were available while building the feature; the full guide explains the splits and limits.
