# Whole-pipeline equivalence eval

Does a change spread over several Dataform models keep every output consumers can see? Each case is a pipeline of `.sqlx` models over declared source tables, a refactor of it, and the list of **observable outputs**. An exposed intermediate model counts as an output; a hidden one does not, so the same edit can be sound with one output list and wrong with another.

```
python tools/pipeline_bench.py                  # development families
python tools/pipeline_bench.py --held-out       # held-out families (final evaluation only)
python tools/pipeline_bench.py --no-declared-columns
```

No LLM runs at evaluation time. Cases are generated in Python with a known answer (`equivalent` or `different`), from 2 models up to 32, and the generator's answers are themselves checked by execution.

## Source and overlap

- Source: written for this eval, so there is no upstream version to pin. The refactor shapes follow the standard rewrites (predicate pushdown, common-subexpression extraction, join reordering, aggregate rollup) in Calcite and SQLSolver, applied across model boundaries.
- Overlap: SQLSolver, R-Bot, VeriEQL, QED and SQL-IQ compare **single queries**; none has multi-model pipelines or exposed intermediates. `tests/test_equivalences.py` has three hand-written pipelines for `prove-tables`; they are not scored here.
- Original cases: all of them (116 development, 18 held out). Adapted cases from public suites: none yet; that track is reported separately when added.

## What is scored

| Level | Evidence | Meaning |
| --- | --- | --- |
| proof | unbounded | `prove_models` proves every output of the new pipeline equal to the old one (layer lemmas, then inlining) |
| bounded | prover counterexample | the solver finds a database where an output differs, replayed through both pipelines |
| executed | agreement/difference on databases | both pipelines run in DuckDB on random databases (0-8 rows per table, NULLs, duplicates) |

Outcomes per case: proved, refuted, unknown, unsupported, timeout, error. A proof of a `different` case or a refutation of an `equivalent` case is **wrong** and must stay 0. Unknown is never wrong. Also reported: pipelines changed, changed outputs, and how many changed outputs were verified by proof (an output whose model and upstream did not change is trivially equal and not counted).

Two results files keep the evidence levels apart: `benchmarks/results/pipeline-equivalence.json` (proof, equivalent cases) and `pipeline-refutation.json` (executed counterexamples, broken cases).

## Families

| Family | Sound variants | Broken variants |
| --- | --- | --- |
| `filter_upstream` | filter moved to the first of a chain of 1-30 staging models; group-key filter before the aggregate; filter on the preserved side of a LEFT JOIN | exposed intermediate or last staging model changes; HAVING moved before the aggregate; filter on the optional side of a LEFT JOIN (two forms) |
| `extract_shared` | 2-8 reports repeating one enrichment now read a shared model; the shared model was already exposed | extra filter, LEFT JOIN or DISTINCT in the extracted model |
| `rollup` | SUM and COUNT rolled up; AVG as SUM/COUNT(col) | average of averages; AVG as SUM/COUNT(*) with NULL values; COUNT(col) rolled up as COUNT(*) |
| `join_staging` | three-way join split into staged models (INNER, LEFT) | a join type changed in either stage |
| `rename_prune` | rename of a hidden or exposed column; unused columns pruned from a hidden model | pruning columns of an exposed model; consumer reads another column |
| `union_split` (held out) | consumer filter pushed into UNION ALL and UNION DISTINCT branches | UNION ALL turned DISTINCT; exposed union loses rows |
| `latest_per_key` (held out) | filter on the partition key moved before ROW_NUMBER | value filter moved before the window; exposed latest model changes |

## Results (2026-10-02)

Baseline before any change to the prover, development families, 116 cases (50 equivalent, 66 different):

| Condition | Proved | Refuted | Unknown | Timeout | Error | Wrong |
| --- | --- | --- | --- | --- | --- | --- |
| Source columns not declared | 32/50 | 66/66 | 18 | 0 | 0 | 0 |
| Source columns declared (the app uses them when a declaration lists columns) | 44/50 | 66/66 | 6 | 0 | 0 | 0 |

Without declared columns the prover rejects `SELECT *` expansions it must make for outer joins, which accounts for the 12 extra unknowns. Supported-subset score: every case ran, so it equals the full score. All 116 pipelines changed; 204 outputs changed, 97 of them verified by proof. Largest pipeline: 32 models. Refutations: 15 by the prover's counterexample, 51 by random-database execution (the solver gives no counterexample for outer-join differences, "UNION shapes differ").

Held-out (union_split, latest_per_key; run once at baseline, declared columns): 3/8 proved, 10/10 refuted, 0 wrong.

Current (2026-10-02, declared columns): **47/50 proved**, 66/66 refuted, 0 wrong; 100 changed outputs verified by proof. `rollup/average_from_sum_count` is now proved: an output combining aggregates (`SUM(total) / SUM(n_amount)`) is regrouped aggregate by aggregate (`regroup_arithmetic.py`) and the result, `SUM(amount) / COUNT(amount)`, matches `AVG(amount)`. Held-out rerun with the new rule: unchanged, 3/8 proved, 10/10 refuted, 0 wrong.

Later on 2026-10-02: **50/50 proved**, 66/66 refuted, 0 wrong; 103 changed outputs verified by proof. The 3 staged LEFT JOIN chains (`join_staging/left_left`) are now proved: a model that reads a staged `LEFT JOIN` model is flattened into one join chain (`outer_join_flatten.py`, see `docs/sqlsolver.md`). Held-out rerun: unchanged, 3/8 proved, 10/10 refuted, 0 wrong.

Unproved sound cases are kept as regression cases in `tests/fixtures/pipeline_equiv/known_gaps.json`; the generator is deterministic, so a case id reproduces exactly.

## Reading the numbers

Doing nothing is not a pass: a refactor that is rejected is counted as unknown or refuted, never proved. Correctness (zero wrong), coverage (proved/refuted/unknown), and analysis of how many outputs actually changed are separate lines.
