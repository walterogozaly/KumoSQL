# Model reuse, containment and aggregate decomposition

Three deterministic Python engines (no LLM at run time) and their evals. All answer by proof first: the algebraic/SMT prover decides, and a positive answer is then re-run on random DuckDB databases that respect the schema (`src/kumosql/random_check.py`); a mismatch counts as **wrong**.

| Eval | Engine | Command | Results file |
| --- | --- | --- | --- |
| Materialized-view and shared-model reuse | `kumosql.model_reuse` | `python tools/mv_reuse_bench.py --all` | `benchmarks/results/mv-reuse-calcite.json` |
| Query containment (set and bag) | `kumosql.containment` | `python tools/containment_bench.py --all` | `benchmarks/results/containment.json` |
| Aggregate decomposition | `kumosql.model_reuse` (rollup identities) | `python tools/decomposition_bench.py --all` | `benchmarks/results/aggregate-decomposition.json` |

Every runner takes `--baseline` (the existing prover alone, before these engines), `--held-out` (the reserved cases; final evaluation only), `--all` and `--json FILE`. Without a flag it runs the development split.

## Materialized-view reuse

`rewrite_over_model(query, model_sql, schema=..., constraints=...)` proposes replacements that read only the model (identity, projection, filter over model columns, join-order changes, aggregate rollup) and returns one **only if** the prover proves query ≡ replacement with the model inlined. `check_replacement` proves or refutes a replacement you supply.

* Source: Apache Calcite 1.37.0 (tag `calcite-1.37.0`, Apache-2.0), `MaterializedViewRelOptRulesTest` and `MaterializedViewSubstitutionVisitorTest`. `tools/extract_calcite_mv.py` downloads the pinned files and writes `tests/fixtures/mv_reuse/calcite_mv_cases.json` (196 cases, SQL unchanged, Calcite's own `ok`/`noMat` verdict kept). A test that calls `ok()` but asserts a plan that never scans `MV0` (Calcite answers from the base table or from empty `VALUES`) is labelled `ok-without-mv` and scored as a cannot-case: `testAggregateMaterializationAggregateFuncs17` and `Funcs20`.
* Original and adapted are separate. Original: the extracted cases above. Adapted: `tests/fixtures/mv_reuse/adapted_cases.json` (29 shared-model cases in Dataform style, written by `tools/make_adapted_mv_cases.py`, scored as source `adapted`).
* Overlap with the other evals: `python tools/mv_overlap.py` finds no case whose query or materialization appears in the SQLSolver, Cosette, QED, R-Bot or SPES fixtures (`tests/fixtures/mv_reuse/overlap.json`). SQLSolver's Calcite pairs come from `RelOptRulesTest`, a different test class.
* A Calcite `noMat` verdict is not proof that no rewrite exists. A rewrite where Calcite says `noMat` is checked, not penalised; none occurred.
* Held out: a quarter of the cases by case-id hash.

| | Reusable cases rewritten | Cannot-cases not rewritten | Unsupported | Wrong |
| --- | --- | --- | --- | --- |
| Baseline, development (existing prover alone) | 8/108 | 32/32 | 0 | 0 |
| Development | 82/108 | 32/32 | 14 | 0 |
| Held out | 27/39 | 9/9 | 3 | 0 |
| All (196 cases, 8 disabled) | 109/147 (supported subset 109/139) | 41/41 | 17 | 0 |

112 of 196 queries were changed; 105 were verified on random databases and 7 could not be run (DuckDB does not read `FLOOR(x TO unit)`). Adapted cases: 20/20 reusable rewritten, 9/9 cannot-cases left alone, 0 wrong (baseline 1/12 on development).

What the proposer reads beyond select-project-join and rollups:

* a filter over a grouped derived table is merged into one grouped query (the outer filter becomes `HAVING`);
* a model it cannot read (a set operation, a join of grouped derived tables) is still tried as the whole answer and as a column subset matched by where each output comes from;
* at the model's own grain a distinct aggregate is read from the model's column, or computed again over the model grouped by its keys; a model key the query fixes to a constant (`WHERE name = 'hello'`) does not count as a finer grain;
* a `COUNT` rolled up as `SUM` of counts gets `COALESCE(.., 0)` whenever the grouping has an empty set (`CUBE`, `ROLLUP`, `GROUPING SETS (.., ())`), since that group exists even over no rows;
* literal `IN` lists.

The `COALESCE` fix matters for correctness: the prover currently proves `COUNT(*)` equal to a `SUM` of grouped counts even over an empty table, so before it five CUBE/ROLLUP rewrites were accepted and failed the random-database check (reported to the prover's soundness owners).

Not read yet: unique/foreign-key joins, outer-join models, `INTERSECT ALL` and branchwise set-operation compensation, union compensation from base tables, correlated subqueries, injective group keys.

## Query containment

`check_containment(q1, q2, schema=..., semantics="set" | "bag", database=...)` answers `contained` (a proof, with the method named), `not_contained` (a stored database where `q1` returns a row `q2` does not return as often), `unknown`, `unsupported` or `timeout`. Set and bag are different questions: `SELECT x FROM t` is set-contained in `SELECT DISTINCT x FROM t` but not bag-contained.

Proof methods reduce containment to an equivalence: equal, pre-filter (restrict `q2` by `q1`'s extra conjuncts), post-filter (reuse engine over an identity model), distinct collapse (bags) and `q1 UNION q2 ≡ q2 UNION q2` (sets). Set operations are taken apart first, with steps that hold for bags: `A EXCEPT B` is within `A`, `A INTERSECT B` within either operand, `A UNION B` within `A UNION ALL B`, and a `UNION ALL` is within another when each branch is contained in its own branch of the other. Literal `IN` lists are read like any other filter.

Cases are generated (`tools/make_containment_cases.py`, 318 cases, labels checked by three-valued evaluation or a database) in families filters, nulls, duplicates, filters-expr, aggregates and joins. Aggregates and joins are held out whole.

| | Decided correctly | Proven | Refuted | Unknown | Unsupported | Wrong |
| --- | --- | --- | --- | --- | --- | --- |
| Baseline, development | 435/582 | 26 | 409 | 147 | 0 | 0 |
| Development | 582/582 | 173 | 409 | 0 | 0 | 0 |
| Held out | 46/54 | 26 | 20 | 6 | 2 | 0 |
| All | 628/636 | 199 | 429 | 6 | 2 | 0 |

## Aggregate decomposition

Given a summary table (a `GROUP BY` at a finer grain) and a coarser target, rebuild the target from the summary: SUM of sums, COUNT as a sum of counts (with `COALESCE(.., 0)` for global aggregates), MIN/MAX, AVG as `SUM(s) / NULLIF(SUM(n), 0)`, COUNT(DISTINCT x) only when `x` is in the summary's grain. Each case has up to three kinds of item: `synth` (rewrite, or decline), `trap` (a tempting wrong rewrite that a database must refute: average of averages, sum of distinct counts, ...) and, for impossible cases, a stored pair of databases with the same summary and different targets, which shows that no function of the summary is right.

Cases: `tests/fixtures/decomposition/cases.json` (`tools/make_decomposition_cases.py`, 44 cases, 59 items). The distinct and empty-null families are held out.

| | Correct | Rewrites found | Impossible declined | Traps refuted | Wrong |
| --- | --- | --- | --- | --- | --- |
| Baseline, development | 16/35 | 2/21 | 7/7 | 7/7 | 0 |
| Development | 34/35 | 20/21 | 7/7 | 7/7 | 0 |
| Held out | 24/24 | 14/14 | 2/2 | 8/8 | 0 |
| All | 58/59 | 34/35 | 9/9 | 15/15 | 0 |

Weighted averages (a per-group `AVG` and its `COUNT`) are rebuilt as `SUM(a * n) / NULLIF(SUM(n), 0)`. Open: standard deviation from a summary (`STDDEV_POP` from sums of squares needs a square-root identity the prover does not do). AVG assumes `/` is fractional division (BigQuery, DuckDB), as the prover already does.

## Prover changes made for these evals

* `eager_aggregation.unnest_grouped_source` handles `SUM` of a per-group `COUNT(x)` and `HAVING`, and replaces calls by identity (equal calls are distinct nodes).
* `algebraic_equivalence._roll_up_aggregate` rolls `COALESCE(SUM(count), 0)` up to the `COUNT`.
* `algebraic_equivalence._mean_times_count` reads `AVG(x) * COUNT(x)` over a grouped derived table as `SUM(x)`.
* `smt_equivalence` treats `COUNT` of any two columns that are never NULL as the same count, and reads `x / NULLIF(y, 0)` as the quotient `x / y`, the same value `AVG` has.
* Negated predicates are read as negations in every dialect (a false proof found by the first containment baseline; PR #241).

## Regression cases

Every failure found while building these is a case: `filters.outside-in-*` and `nulls.*` (a disjunction lost its parentheses in the pre-filter and was proven contained), the NaN comparison in the random harness, and the AVG count-key lookup. Tests: `tests/test_model_reuse_evals.py` (fast floors, slow full-corpus floors, regressions).

Residual filters that are disjunctions are parenthesized when combined (`a = 1 AND (b = 2 OR c = 3)`); without it the prover rejected the replacement and the rewrite was lost.
