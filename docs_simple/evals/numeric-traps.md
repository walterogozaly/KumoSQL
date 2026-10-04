# Number and error traps for the prover

[Simple eval index](README.md) · [Full reference](../../docs/evals/numeric-traps.md)

Numbers in BigQuery do not behave like the numbers in school. A whole number past 9,007,199,254,740,992 loses its last digits when it meets a decimal. `0.1 + 0.2` is not `0.3`. Dividing by zero is an error, not infinity, unless you ask for `IEEE_DIVIDE`. And an error can happen on a row that your `WHERE` would have thrown away, because BigQuery does not promise to filter first.

This suite is a set of hand-written query pairs built around exactly these traps. Some are the same query written two ways (the prover should say so). Some differ only on an edge such as a huge integer or `NaN` (the prover must never say they match). Some are rewrites that could make a query start failing, for example removing the `IF` that kept a division away from zero.

## What it checks

Decimal (`NUMERIC`) numbers have their own trap: they keep nine digits after the point, so a product or a quotient is rounded back to nine digits. `n / 3 * 3` is not `n` (with `n = 0.000000001` the quotient rounds to 0), `ROUND(n * m, 9)` is the same number as `n * m`, and `NUMERIC '1.50'` is the same value as `NUMERIC '1.5'`. The prover now works these out exactly, as long as it knows how many decimal places each value has (a column declared `NUMERIC`, a quoted decimal cast to it, and `+ - * /`, `CAST` and `ROUND` over those). A sum, a `CASE` or a floating-point value has no known number of places, so those pairs stay unknown.

A concrete example: `SELECT IF(y = 0, 0, x / y) FROM t WHERE y <> 0` and `SELECT x / y FROM t WHERE y <> 0` return the same rows on every database where neither fails, but only the first keeps the division away from a zero `y`. BigQuery may evaluate the division before the filter, so the second can fail on a row the filter would drop, and a rewrite from the first to the second is not safe. The prover now says so, instead of proving the pair equal while quietly assuming errors never happen.

The score is how many sound pairs the prover proves, how many trap pairs it refuses to prove, and whether each rewrite that adds or removes an error is described correctly. A wrong answer is a proof or a refutation that contradicts what BigQuery's documentation says.

```sh
python tools/numeric_traps_bench.py
```

## Adding decimals in a different order

Adding decimal numbers is not exactly the same in every order: 10000000000000000 + 1 + 1 gives 10000000000000000, but 1 + 1 + 10000000000000000 gives 10000000000000002. BigQuery does not promise which order `SUM` uses for floating-point columns, so a rewrite that merely moves where the additions happen (summing per group first and then summing the group totals, or turning `SUM(f) + SUM(g)` into `SUM(f + g)`) can change the last digits. Twenty of the pairs test this. The prover proves `SELECT SUM(f) FROM t` equal to the same query written again and says so in a narrow assumption (the identical sum returns the same value both times). It also says nothing is assumed when every summed value is a whole number or an exact decimal. For the same rows reached a different way (a filter written differently, branches swapped) it proves at most with the full "sum does not depend on row order" assumption listed, and for regrouped sums it does not prove anything. The limits: the claim that BigQuery has no fixed order for floating-point sums comes from this repo's own notes, not from the BigQuery documentation, so those labels are marked unverified, and a column of a derived table over a `UNION` is not recognised as whole numbers.

## Adding up groups

One more trap is about sums. `SELECT y, SUM(x) FROM t GROUP BY y HAVING y > 0` and `SELECT y, SUM(x) FROM t WHERE y > 0 GROUP BY y` return the same rows, but the first also adds up the group `y = 0` before throwing it away, and two huge values in that group overflow a 64-bit integer. So the rewrite is safer, and the reverse is not. 17 pairs check that the prover tells these apart group by group (a `HAVING` moved into `WHERE` and back, a filter spelled another way, `SUM(DISTINCT)`, a join, a window), and that it stays unknown when a sum is split into partial sums and added up again.

## What to keep in mind

- The pairs were written by the person who changed the prover, from the issue's list of traps. They were fixed before the change and a quarter was held out, but this is a safety net, not an independent test.
- A few labels rest on behaviour the author could not confirm in the documentation (for example how a decimal literal is rounded, and that multiplying or dividing two `NUMERIC` values rounds half away from zero). Those cases say so, and "unknown" is an acceptable answer for them. The prover assumes that rounding rule, so an answer that depends on it is only as good as the rule.
- The 17 sum pairs were added later with five held out (not tuned on), but they were written by the person who wrote the check they test. They assume that BigQuery adds up a group that `HAVING` then drops (an optimizer may skip it) and that `SUM(DISTINCT)` and window sums fail on overflow like `SUM`; neither is confirmed in the documentation, and the cases say so. The two window pairs stay unknown because the prover does not prove a filter moved across a window.
- The sixteen decimal cases were added with the rounding model; four are held out, but their answers were seen during development, so treat the held-out score for them as "tuned on test".
- The prover still assumes no `NaN` appears in floating-point columns, and it does not model the double that a floating-point `/` returns. Those pairs stay unknown or are proved with the assumption listed.

See the full reference for the recorded scores and the list of cases.
