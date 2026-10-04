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

## Adding decimals in a different order

Adding decimal numbers is not exactly the same in every order: 10000000000000000 + 1 + 1 gives 10000000000000000, but 1 + 1 + 10000000000000000 gives 10000000000000002. BigQuery does not promise which order `SUM` uses for floating-point columns, so a rewrite that merely moves where the additions happen (summing per group first and then summing the group totals, or turning `SUM(f) + SUM(g)` into `SUM(f + g)`) can change the last digits. Twenty of the pairs test this. The prover proves `SELECT SUM(f) FROM t` equal to the same query written again and says so in a narrow assumption (the identical sum returns the same value both times). It also says nothing is assumed when every summed value is a whole number or an exact decimal. For the same rows reached a different way (a filter written differently, branches swapped) it proves at most with the full "sum does not depend on row order" assumption listed, and for regrouped sums it does not prove anything. The limits: the claim that BigQuery has no fixed order for floating-point sums comes from this repo's own notes, not from the BigQuery documentation, so those labels are marked unverified, and a column of a derived table over a `UNION` is not recognised as whole numbers.

## What to keep in mind

- The pairs were written by the person who changed the prover, from the issue's list of traps. They were fixed before the change and a quarter was held out, but this is a safety net, not an independent test.
- The held-out quarter has no pair about errors, so error handling is checked only on the development pairs.
- A few labels rest on behaviour the author could not confirm in the documentation (for example how a decimal literal is rounded). Those cases say so, and "unknown" is an acceptable answer for them.
- The prover still assumes no `NaN` appears in floating-point columns and does not do exact decimal rounding. Those pairs stay unknown or are proved with the assumption listed.

See the full reference for the recorded scores and the list of cases.
