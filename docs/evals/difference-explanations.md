# Verified difference explanations

This eval measures whether KumoSQL can describe a real query difference with a short predicate, such as `status IS NULL`. Each counted pair was independently refuted by its source eval, and every reported predicate has both a replayed witness and proofs from the SMT and algebraic provers after filtering the predicate's rows.

## Development result

In the fixed development samples, **29/35 (82.9%)** of the independently refuted pairs that compile to one select-project-join block had a verified predicate of at most three atoms. No unverified predicate was counted. The full syntactic SPJ sample was **29/152 (19.1%)**: 115 pairs compiled into multiple branches, which this first row-local implementation does not explain, and two more used an unsupported `LEFT JOIN ... USING` form. The broader rate is below the 60% goal in issue #512; reaching that goal across all syntactic SPJ cases requires explaining those branch-expanded joins too.

| Source | Source-refuted SPJ sample | One-block | Explained | Multi-branch or unsupported |
| --- | ---: | ---: | ---: | ---: |
| Singh and Bedathur, development split | 49 | 15 | 12 | 34 |
| VeriEQL LeetCode, fixed sample | 50 | 17 | 14 | 33 |
| VeriEQL Literature | 3 | 3 | 3 | 0 |
| Unsafe rewrite detection, generated sample | 50 | 0 | 0 | 50 |
| Pipeline refutation | 0 | 0 | 0 | 0 |
| **Total** | **152** | **35** | **29** | **117** |

Among the 35 one-block pairs, six had no predicate of at most three atoms. The 115 multi-branch cases were declined as a set operation; the two remaining unsupported cases used `LEFT JOIN ... USING`. The pipeline development cases supplied no direct one-block SPJ output pair after inlining. The table counts only cases whose independent source refutation was confirmed, not every query offered to the sources.

## Sources and split

- Singh and Bedathur comes from its pinned 2,800-pair source. The development run uses the source's hash-selected dev split. Its held-out fifth is evaluated once at final scoring and is never used to change the implementation.
- VeriEQL LeetCode uses a fixed hash sample of up to 50 SPJ pairs whose published `NEQ` counterexamples replay on DuckDB. Literature has five SPJ candidates, three with replayable `NEQ` witnesses. VeriEQL publishes no held-out split, so these cases are development-only.
- Unsafe rewrite detection uses up to 50 non-held-out generated SPJ pairs from `unsafe --count 20 --seed 1`; the source oracle and replay must also confirm the prover's refutation.
- Pipeline refutation checks development cases that have one observable output, inlines the project models, and then applies the same SPJ filter. No source SQL or counterexample databases are stored in this repository.

The fixed samples use a SHA-1 ordering of source and pair text. The benchmark excludes pairs that are not independently refuted. The `single-block` score further excludes pairs whose query compilation expands to multiple branches or uses a shape the checker declines. The broader syntactic-SPJ rate is reported alongside it so branch expansion does not disappear from view.

## Held-out result

The source-provided Singh and Bedathur held-out fifth contained nine independently refuted SPJ pairs in this fixed sample. **3/3** pairs compiling to one block received verified predicates; the other six compiled into multiple branches, for **3/9 (33.3%)** across the full syntactic-SPJ sample. The unsafe-rewrite and pipeline sources supplied no eligible held-out SPJ pairs in this run. VeriEQL has no held-out partition. The 3/3 result is a very small sample and does not meet the 60% goal across all syntactic SPJ cases.

## Limits and rerun

The checker models one row per input table to find a candidate predicate, then verifies the witness on DuckDB and proves the filtered queries equal with two provers. Grouped pairs have their own conservative proof path; this eval focuses on SPJ. The source samples are fixed diagnostics, not full-suite scores. No separate human readability review of 50 predicates was performed.

Run the development and source-held-out samples with:

```powershell
python tools/difference_bench.py --split all --write-results
```

The command updates `benchmarks/results/difference-explanations.json` and regenerates the README scoreboard. Held-out scoring uses only source-provided held-out cases; VeriEQL is not counted as held out.

See the [Singh and Bedathur](singh-bedathur.md), [VeriEQL](verieql.md), [fuzzing](fuzzing.md), and [pipeline equivalence](pipeline-equivalence.md) eval pages for the source suites and their independent refutation checks.
