# LIMIT and ORDER rule target

[Rule-level fuzzing](rule-fuzzing.md) checks each rewrite firing against DuckDB. The `limits` target gives the
`limit_rule` normalizer entry point direct inputs that reach its ORDER BY and cut rewrites:

```bash
python tools/rule_fuzz.py run --corpus target:limits --seed 496 --count 240 --jobs 3 --out limits.json
python tools/rule_fuzz.py report limits.json
```

The target covers removing unread orderings, removing duplicate sort keys, merging nested top-k cuts, removing
redundant cuts from `UNION ALL` branches, and lifting an ordered cut out of a plain projection. The typed cast
ordering rule has focused direct tests in `tests/test_limit_rules.py`; the normalizer's earlier cast pass handles
those inputs before `limit_rule` is reached, so they cannot serve as rule-fuzzer firings.

Guard cases keep an ordering when an outer cut reads it, keep distinct sort expressions, and avoid merging cuts
when the order leaves projected values tied, the inner and outer directions differ, or an inner `UNION ALL`
branch cut is shorter than the outer `LIMIT + OFFSET`. A `DISTINCT` projection also remains in place when an
ordered cut cannot be lifted through it.

This is a reachability sample, not a proof that every cut shape is sound. The report's checked counts and the
existing direct tests in `tests/test_limit_rules.py` provide the detailed evidence for individual cases.
