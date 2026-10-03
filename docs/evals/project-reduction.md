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

**Converted (RESULTS_CONVERTED_DEV dev, 64 held out).** Every [table-minimization](table-minimization.md) case is written out as a Dataform project: its sources become declarations in a `raw` schema, every table a `.sqlx` file that names tables and sources with `ref()`, and its protected tables are the kept outputs. A hash of the case id mixes in Dataform features, so the reducer meets them in combination rather than one at a time:

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

**Real (RESULTS_REAL_DEV dev, RESULTS_REAL_HELD held out).** The eight open-source Dataform projects in `tests/fixtures/bq_corpora` (licences there), with each of up to twelve final actions kept alone and all of them kept together. One case in five, by a hash of its id, is held out. They hold operations scripts, incremental tables, `js` blocks, `includes/` constants, project variables, config assertions and `dependencies`.

## Checking

Each patch is applied to a copy of the project.

- **Converted:** `git apply --check` must accept the patch. The harness compiles both projects with its own small compiler (`ref()` to the action's name, project variables to their values, `self()` to the action, incremental branches dropped) and compares every kept output, and every assertion both projects still have, on the targeted databases, 60 random ones and every trap witness of the case, on DuckDB with the optimizer off (the [table-minimization](table-minimization.md) checker). A difference, a kept output that is missing or a project that does not compile is **wrong**.
- **Real:** there is no data, so the check is `git apply --check` plus the reducer's own re-proof of every kept output of the patched project against the original (a reduction that does not re-prove is counted `unverified`).
- **Negative control:** `tests/test_reduction_bench.py` renders the first trap of six dev cases as a "reduced" project; the checker counts every one wrong, and a project that lost a kept output too.

## Scores

RESULTS_SECTION

## Limits

- The converted cases come from the table-minimization generator: synthetic pipelines over e-commerce sources, written for KumoSQL. The real projects are few (eight) and mostly simple; their reductions are proved, not executed.
- Complexity is `kumosql.project_reduction.project_score`: the sqlfluff structure of every action plus one per action. Quality compares the converted tables (assertions left out) with the minimization case's reference.
- Proofs are as sound as KumoSQL's prover; "0 wrong" means no patch changed a kept output on the check databases.
