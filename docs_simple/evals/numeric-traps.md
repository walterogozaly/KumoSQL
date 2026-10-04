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

## What to keep in mind

- The pairs were written by the person who changed the prover, from the issue's list of traps. They were fixed before the change and a quarter was held out, but this is a safety net, not an independent test.
- The held-out quarter has no pair about errors, so error handling is checked only on the development pairs.
- A few labels rest on behaviour the author could not confirm in the documentation (for example how a decimal literal is rounded). Those cases say so, and "unknown" is an acceptable answer for them.
- The prover still assumes no `NaN` appears in floating-point columns and does not do exact decimal rounding. Those pairs stay unknown or are proved with the assumption listed.

See the full reference for the recorded scores and the list of cases.
