# Testing each rewrite on its own

[All simple guides](README.md) · [Full reference](../docs/rule-fuzzing.md)

KumoSQL proves two queries equal by first rewriting both into a standard form and comparing the results. If one rewrite is wrong, two queries that return different rows can end up looking the same, and the proof is false. Testing pairs of queries finds such a rule only if it happens to build a pair that uses it. This tool tests each rewrite directly.

## An example

For the query `SELECT q.c FROM (SELECT MIN(x) AS k, 1 AS c FROM t) AS q`, one rewrite removes the unread column `k`. The tool runs the query before and after that single rewrite on small databases full of empty tables, NULLs, duplicates and ties. Here the "after" query no longer has its aggregate, so it returns one row per input row instead of one row. The two differ, so the tool reports the rewrite, shrinks the example to a few rows, and a developer fixes the rule.

Before it reports anything, the tool rules out noise: the same difference must appear with DuckDB's optimizer off, with the table rows in reverse order, and with every `LIMIT` made fully ordered, so a choice among tied rows is never blamed on a rule.

## What it has found so far

Four wrong rewrites on the first runs: two in the rules that handle provably empty tables, one in the rule that drops unread columns of a `UNION ALL`, and one that dropped a grand-total grouping inside `EXISTS`. All three are fixed and kept as tests. The running list is on the workstream issue linked from the full reference.

## Looking inside the big rules

Some rules are really a bundle of smaller rewrites: one name in the report, several different shapes inside. Counting that name as "fired" can hide a shape that was never tried. Four generators (`distinct_variants`, `eager_variants`, `aggregate_variants`, `keyed_variants`) write one query for each shape and one near miss for each safety check, for example a `LIMIT` that would see repeated rows, a `COUNT(*)` that would count them, or a key that may be NULL. A small test makes sure each shape still triggers its rewrite, so a later edit cannot quietly stop it. The full list is in the [full reference](../docs/rule-fuzzing.md#variants-inside-one-rule).

## Limits

- A rule that passes is not proven right. It only survived the queries and databases the tool tried, and a rule that never fires has not been tested at all; the report counts how often each rule fired.
- The checking engine is DuckDB, not BigQuery. Where they behave differently the tool skips the case, and each difference is reviewed by hand.
