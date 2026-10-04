# Project reduction eval

Given a whole Dataform project and the outputs that must stay, how small a project can KumoSQL prove still produces them? [Project reduction](../project-reduction.md) (`kumosql.project_reduction.reduce_project`) returns a patch on the project's files. A kept output must keep its name and give exactly the same output: the same column names in the same order and the same bag of rows, on every database. Everything else may be deleted, folded, merged, factored into a shared table or rewritten.

```
python tools/reduction_bench.py --jobs 4                  # dev split, both families
python tools/reduction_bench.py --family real             # the open-source Dataform projects only
python tools/reduction_bench.py --split held_out --jobs 4 # final evaluation only
python tools/reduction_bench.py --only gen-0001 --json out.json
```

No LLM runs at evaluation time.

## Cases

**Converted (270 dev, 64 held out).** Every [table-minimization](table-minimization.md) case is written out as a Dataform project: its sources become declarations in a `raw` schema, every table a `.sqlx` file that names tables and sources with `ref()`, and its protected tables are the kept outputs. A hash of the case id mixes in Dataform features, so the reducer meets them in combination rather than one at a time:

| Feature | Share | What it adds |
| --- | --- | --- |
| `assert_kept` | 1 in 2 | A standalone assertion on a kept output (it must stay) |
| `assert_inner` | 1 in 2 | A standalone assertion on an intermediate table (dropped and listed, or kept when awaited) |
| `config_assertion` | 1 in 2 | `assertions: {nonNull: [...]}` in an intermediate's config (it must stay proved equal while it survives) |
| `vars` | 1 in 3 | One string literal read from `${dataform.projectConfig.vars.v1}` (an unknown constant to the prover) |
| `incremental` | 1 in 5 | One intermediate is an incremental table (kept as written, with everything it reads) |
| `dependency` | 1 in 3 | A kept output lists the intermediate's assertion in `dependencies` |
| `views` | 1 in 2 | Every other table is a view |

The case keeps its own split, so the 64 held-out minimization cases are held out here too.

**Real projects (40 dev, 6 held out).** The eight open-source Dataform projects in `tests/fixtures/bq_corpora` (licences there), with each of up to twelve final actions kept alone and all of them kept together. One case in five, by a hash of its id, is held out. They hold operations scripts, incremental tables, `js` blocks, `includes/` constants, project variables, config assertions and `dependencies`.

**Jaffle Shop (6 dev).** dbt Labs' Jaffle Shop (`tests/fixtures/jaffle_shop`, pinned and licensed there), written as a Dataform project the way `tools/jaffle_shop_bench.py` writes it, with each mart, both marts and each staging model kept. Its seed CSVs are real data, so these reductions are executed as well as proved.

## Checking

Each patch is applied to a copy of the project.

- **Converted:** `git apply --check` must accept the patch. The harness compiles both projects with its own small compiler (`ref()` to the action's name, project variables to their values, `self()` to the action, incremental branches dropped) and compares every kept output, and every assertion both projects still have, on the targeted databases, 60 random ones and every trap witness of the case, on DuckDB with the optimizer off (the [table-minimization](table-minimization.md) checker). A difference, a kept output that is missing or a project that does not compile is **wrong**.
- **Real:** there is no data, so the check is `git apply --check` plus the reducer's own re-proof of every kept output of the patched project against the original (a reduction that does not re-prove is counted `unverified`).
- **Negative control:** `tests/test_reduction_bench.py` renders the first trap of six dev cases as a "reduced" project; the checker counts every one wrong, and a project that lost a kept output too.

## Scores

Measured 2026-10-04 (`python tools/reduction_bench.py --jobs 4`; the complexity score changed on 2026-10-04, so the real-project and Jaffle Shop totals moved a little: comma-joined FROM items count as joins and `IF()` as a CASE). Complexity is summed over the cases; "dropping alone" is `reduce-project --drop-only`, which deletes what the kept outputs do not need and rewrites nothing.

| Split | Cases | Reduced | Wrong | Re-proved | Complexity | Dropping alone | Beyond dropping |
| --- | --- | --- | --- | --- | --- | --- | --- |
| Dev, converted | 270 | 240 | 0 | 270 | 5,056.5 -> 3,547.0 (-29.9%) | -14.2% | 220 |
| Dev, real projects | 40 | 36 | 0 | 40 | 5,207.2 -> 3,801.9 (-27.0%) | -26.8% | 7 |
| Dev, Jaffle Shop | 6 | 6 | 0 | 6 | 198 -> 61 (-69.2%) | -59.1% | 6 |
| **Dev, all** | **316** | **282** | **0** | **316** | **10,461.7 -> 7,409.9 (-29.2%)** | **-21.3%** | **233** |
| Held out, converted | 64 | 59 | 0 | 64 | 1,144.5 -> 821.5 (-28.2%) | -10.2% | 56 |
| Held out, real projects | 6 | 6 | 0 | 6 | 1,002.9 -> 773.7 (-22.9%) | -22.8% | 1 |

- **Executed:** on DuckDB, 220 converted dev projects agree with the original on every check database and 50 are the same SQL; all 6 Jaffle Shop reductions agree on the seeds and 60 random databases.
- **Quality:** the converted tables reach 65.6% of the minimization reference's reduction (66.3% held out), against 80% for the [table minimizer](table-minimization.md) on the same cases as plain tables. The difference is the Dataform features: incremental tables and models with a project variable in a value stay as written, assertions keep their tables or are dropped, and a rewrite has to lower the score.
- **Proof rate:** 787 of the 1,267 steps the search tried were proved (62.1%); the rest were rejected and listed.
- **Actions:** 4,329 -> 2,557 on the dev split (dropping alone: 3,106). 302 assertions over removed or rewritten tables were dropped, each listed with the reason.
- **Shared tables:** no dev case gained a new shared table, and one held-out case reused an existing table for a repeated query. The converted cases repeat logic as whole tables (merged instead), not as subqueries; factoring is covered by `tests/test_project_reduction.py` and `tests/test_table_minimizer.py`.
- **Real projects:** most of the gain is dropping. snowplow-web is mostly operations scripts, which stay as written; one of its queries is simplified in all five of its cases. In wintermi-imdb, two staging tables are folded into the kept report, which inherits their assertion gates.
- **Runtime:** median 3.8 s per case, max 73 s, 1,987 s in total on 4 workers (60 s search limit for converted cases, 120 s for real projects and Jaffle Shop), re-proof included; checking took 411 s.

The first full run found one wrong reduction, `gen-0173`. A model compared a column with `"${dataform.projectConfig.vars.v1}"`, and the prover read that as one more string, different from `'paid'`, so a filter looked contradictory. Such models are now kept exactly as written, and the same problem in other KumoSQL features was reported to their owners. The run also showed that the check after writing the patch back skipped tables that an action kept as written reads (`verify_tables` now proves them too), and that a declaration named `events_*` was deleted while it was still read (that patch was not verified, so it was never returned).

## Limits

- The converted cases come from the table-minimization generator: synthetic pipelines over e-commerce sources, written for KumoSQL. The real projects are few (eight) and mostly simple; their reductions are proved, not executed.
- Complexity is `kumosql.project_reduction.project_score`: the sqlfluff structure of every action plus one per action. Quality compares the converted tables (assertions left out) with the minimization case's reference.
- Proofs are as sound as KumoSQL's prover; "0 wrong" means no patch changed a kept output on the check databases.
