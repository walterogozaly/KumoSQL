# Number and error traps for the prover

[Simple eval index](README.md) · [Full reference](../../docs/evals/numeric-traps.md)

Numbers in BigQuery do not behave like the numbers in school. A whole number past 9,007,199,254,740,992 loses its last digits when it meets a decimal. `0.1 + 0.2` is not `0.3`. Dividing by zero is an error, not infinity, unless you ask for `IEEE_DIVIDE`. And an error can happen on a row that your `WHERE` would have thrown away, because BigQuery does not promise to filter first.

This suite is a set of hand-written query pairs built around exactly these traps. Some are the same query written two ways (the prover should say so). Some differ only on an edge such as a huge integer or `NaN` (the prover must never say they match). Some are rewrites that could make a query start failing, for example removing the `IF` that kept a division away from zero.

## What it checks

A concrete example: `SELECT IF(y = 0, 0, x / y) FROM t WHERE y <> 0` and `SELECT x / y FROM t WHERE y <> 0` return the same rows on every database where neither fails, but only the first keeps the division away from a zero `y`. BigQuery may evaluate the division before the filter, so the second can fail on a row the filter would drop, and a rewrite from the first to the second is not safe. The prover now says so, instead of proving the pair equal while quietly assuming errors never happen.

The score is how many sound pairs the prover proves, how many trap pairs it refuses to prove, and whether each rewrite that adds or removes an error is described correctly. A wrong answer is a proof or a refutation that contradicts what BigQuery's documentation says.

```sh
python tools/numeric_traps_bench.py
```

A second example is about sums. `SELECT y, SUM(x) FROM t GROUP BY y HAVING y > 0` and `SELECT y, SUM(x) FROM t WHERE y > 0 GROUP BY y` return the same rows, but the first also adds up the group `y = 0` before throwing it away, and two huge values in that group overflow a 64-bit integer. So the rewrite is safer, and the reverse is not. The pairs check that the prover tells these apart group by group, and that it stays unknown when a sum is split into partial sums and added up again.

## What to keep in mind

- The pairs were written by the person who changed the prover, from the issue's list of traps. They were fixed before the change and a quarter was held out, but this is a safety net, not an independent test.
- The first batch of pairs has no held-out pair about errors. The 17 pairs about adding up groups (a `HAVING` moved into `WHERE` and back, a filter spelled another way, `SUM(DISTINCT)`, a join, a window) were added later with five held out, but they were written by the person who wrote the check they test.
- The sum pairs assume that BigQuery adds up a group that `HAVING` then drops (an optimizer may skip it) and that `SUM(DISTINCT)` and window sums fail on overflow like `SUM`. Neither is confirmed in the documentation, and the cases say so. The window pairs stay unknown because the prover does not prove a filter moved across a window.
- A few labels rest on behaviour the author could not confirm in the documentation (for example how a decimal literal is rounded). Those cases say so, and "unknown" is an acceptable answer for them.
- The prover still assumes no `NaN` appears in floating-point columns and does not do exact decimal rounding. Those pairs stay unknown or are proved with the assumption listed.

See the full reference for the recorded scores and the list of cases.
