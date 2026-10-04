# Whole-pipeline equivalence eval

[Plain-language version](../../docs_simple/evals/pipeline-equivalence.md)

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
- Original cases: all of them (116 development, 18 held out). Adapted cases from public suites: none in the generated families; the [Jaffle Shop track](#jaffle-shop-a-real-dbt-project) below runs a real dbt project and is reported separately.

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

Current (2026-10-02, declared columns): **47/50 proved**, 66/66 refuted, 0 wrong; 100 changed outputs verified by proof. `rollup/average_from_sum_count` is now proved: an output combining aggregates (`SUM(total) / SUM(n_amount)`) is regrouped aggregate by aggregate (`regroup_arithmetic.py`) and the result, `SUM(amount) / COUNT(amount)`, matches `AVG(amount)`. The 3 staged LEFT JOIN chains (`join_staging/left_left`) were the unknowns; reading through derived outer joins (`outer_join_flatten.py`, see `docs/sqlsolver.md`) now proves them, so the development set is **50/50 proved**, 66/66 refuted, 0 wrong. Held-out rerun with the new rule: unchanged, 3/8 proved, 10/10 refuted, 0 wrong.

Unproved sound cases are kept as regression cases in `tests/fixtures/pipeline_equiv/known_gaps.json`; the generator is deterministic, so a case id reproduces exactly.

## Reading the numbers

Doing nothing is not a pass: a refactor that is rejected is counted as unknown or refuted, never proved. Correctness (zero wrong), coverage (proved/refuted/unknown), and analysis of how many outputs actually changed are separate lines.

## Jaffle Shop: a real dbt project

The generated families above are written for this eval. The Jaffle Shop track runs the same questions on a real project: dbt Labs' `jaffle_shop_duckdb` (three seed CSVs, three staging views, two marts, 20 YAML tests).

```
python tools/jaffle_shop_bench.py                      # every track, 10 random databases (30 for the refactors)
python tools/jaffle_shop_bench.py --track refactors --only pivot
python tools/jaffle_shop_bench.py --write-results      # writes the three results files below
```

### Source, pin and adaptations

- Source: `dbt-labs/jaffle_shop_duckdb`, `duckdb` branch, commit `20cc904370818ceab08cf19f8fd196e9e81def88`, Apache-2.0. The models, both `schema.yml` files, `dbt_project.yml`, the seeds and the licence are copied unchanged into `tests/fixtures/jaffle_shop/`; `source.json` there holds the commit and the SHA-256 of every file, and the eval checks them before it runs.
- Adapted, not upstream: dbt is not run. A small renderer in the harness handles exactly the Jinja the project uses (`{# #}` comments, a `{% set %}` list, a `{% for %}` loop, `{{ ref('x') }}`, the `-` whitespace controls) and refuses anything else; when `jinja2` is installed its output is checked to be identical to Jinja's. Seeds are loaded with the types dbt infers (integers, a date, strings). The dbt build creates the rendered models in `ref` order, as views and tables the way `dbt_project.yml` configures them.
- For KumoSQL the rendered models are written as a Dataform project (`{{ ref('x') }}` becomes `${ref("x")}`, each seed a declaration with its columns) and loaded with `load_sqlx_project`, as a user would load it.
- Written for this eval: `expected_lineage.json` (the sources, transform and seed sources of every output column, written by hand from the SQL) and the 15 refactors in the harness. These are kept apart from the upstream cases (the build, graph and rewrite tracks).
- Overlap: no other eval scores this project or any dbt project. Spider 2.0's dbt tasks (inventory E08) are not scored because their archives are on Google Drive. The refactor shapes are like the generated families above (join type changed, `COUNT(*)` for `COUNT(col)`, a model extracted), but the SQL is the project's.

### What is scored

| Track | Check | Result (2026-10-03) |
| --- | --- | --- |
| dbt build | the 20 upstream YAML tests (`unique`, `not_null`, `accepted_values`, `relationships`) pass on the dbt build of the seeds; the renderer matches `jinja2` | 20/20 pass; renderer identical |
| Graph | KumoSQL's model graph equals dbt's `ref` graph | 8/8 edges, 0 extra, no gaps |
| Lineage | every output column has the expected one-hop sources, transform and seed sources (`explain_lineage`, `trace_column`); the column lists match the DuckDB build | 27/27 columns |
| Loader round trip | every model rebuilt in DuckDB from the SQL KumoSQL loaded returns the dbt build's rows | 5 models equal on the seeds and 10 random databases |
| Rewrites | each of the 8 rewrite rules on its own and the whole cleanup in canonical order, on each of the 5 models; the pipeline is rebuilt with the rewritten model and every model's output compared on the seeds and 10 random databases (NULLs, duplicate and dangling keys, empty tables). Any change is wrong | 45/45 keep every output, **0 wrong**: 10 change the query (inlining the single-use CTEs; all 10 proven by the rule's own check), 5 change the layout only, 30 decline (the upstream SQL has no trivial predicates, redundant parentheses, duplicate or unused CTEs) |
| Output comparison | `plan_output_comparison` on the unchanged pipeline built twice, and on the dbt build against KumoSQL's: the joined query, per-side snapshots (`compare_snapshots`) and a keyed drill-down | 30/30 checks report no difference |
| Authored refactors | 8 equivalent and 7 breaking refactors; `prove_models` must prove or say unknown; a breaking one is refuted by the prover's counterexample (searched on databases built for the inlined queries) replayed through both pipelines, or by random-database execution. Every label is checked by execution on the seeds, random databases and one hand-written edge database (`edge_rows`: an order whose payments of one method all have NULL amounts, a repeated order id, a payment of a missing order), so no breaking refactor depends on a timed solver search to be shown different; and the output-comparison API must agree with the direct comparison on the seeds | **4/8 proved, 7/7 refuted, 0 wrong**; labels and comparison API agree on all 15 |

A difference in DuckDB counts only when it is still there with DuckDB's optimizer off (`kumosql.duckdb_load.run_unoptimized`). Floats are compared to 6 decimal places (`SUM` over `FLOAT64` has no fixed order in BigQuery either).

The refactors: proved are the payment pivot extracted into its own model, the pivot written with a simple `CASE`, the final `LEFT JOIN` of `orders` written as a `RIGHT JOIN`, and the two `LEFT JOIN`s of `customers` swapped. Unknown (regression cases in `tests/test_jaffle_shop_bench.py`, `KNOWN_UNPROVED`): the staging logic inlined into `customers`, lifetime value summed from the `orders` mart, the inner join between payments and orders inside `customers` (unmatched payments only formed a NULL customer group the final join never matches), and the order counts taken from the `orders` mart; each stops at "no row-preserving mapping between the queries was found". Refuted, all by a replayed counterexample: an inner join that drops orders without payments, `COALESCE(SUM(CASE ... THEN amount END), 0)` for the pivot, `COUNT(*)` and `COUNT(DISTINCT order_id)` for `COUNT(order_id)`, returned orders filtered out of the exposed staging model, an inner join that drops customers without orders, and `COALESCE` on the lifetime value. Four of the seven breaks (the inner join to payments, the pivot `COALESCE`, both counts) do not show on the upstream seed data, and all 20 upstream tests still pass on them: the project's own tests would not catch them.

Held-out cases (a fifth of each track by SHA-1 of the case id; nothing in KumoSQL was changed for this eval, so the whole run is the baseline): rewrites 8/8, lineage 5/5, refactors 1/1 refuted (no equivalent refactor falls in the held-out fifth).

Results files: `benchmarks/results/jaffle-shop.json` (executed: rewrites, with the graph, lineage, build and comparison results), `jaffle-shop-refactors.json` (proof) and `jaffle-shop-refutation.json` (executed counterexamples). The test (`tests/test_jaffle_shop_bench.py`) runs every track on fewer random databases; six of the refactors run in the fast tier and the rest in the slow tier. The refactors run on 8 random databases (seed 23) plus the edge database; the pivot `COALESCE` break first shows on random database 14 of that seed, so before the edge database was added the test passed only when the prover's 5 s counterexample search finished, and failed on a loaded machine (`test_every_breaking_refactor_shows_on_the_edge_database` keeps it that way). A loaded machine can still change which route is credited in the refutation row (prover or execution), never the verdict.
