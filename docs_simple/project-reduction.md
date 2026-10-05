# Shrinking a Dataform project to the outputs you need

[All simple guides](README.md) · [Full reference](../docs/project-reduction.md)

Name the tables of a Dataform project that people actually use. KumoSQL proposes a smaller project that still builds each of them under the same name, with the same columns in the same order and the same rows. It returns a patch for review; it changes your files only if you ask it to.

## In the browser

Open **Reduce** in the sidebar with a project loaded. Search the actions, tick the ones people use, and press **Run**. The page shows whether the smaller project was proved, how many actions and how much complexity were removed, what each kept output's check found, what was deleted or changed and why, and the patch to copy or download. It works on a copy, so your files do not change. **Drop only** deletes what is not needed and rewrites nothing. The [full guide](../docs/project-reduction.md) lists the options and the API.

## What can change

- Tables, views, declarations and assertions that no kept output needs are deleted.
- A staging table read by one model can be folded into that model, two tables that return the same rows merged, and columns nobody reads removed.
- A query written in several models can move into one new shared table.
- Each remaining query can be simplified.

Every change is kept only when the prover shows each kept output is still the same. A change it cannot prove is rejected and listed. After the patch is written, KumoSQL loads the patched project again and proves each kept output against the original before returning it.

## Try it

From a project checkout:

```sh
python -m kumosql reduce-project path/to/project --keep orders --keep customers --patch reduce.diff
```

Replace the path and names. The JSON output lists what was removed, changed, added and kept as written, with a verdict per kept output. `--drop-only` only deletes what the kept outputs do not need and rewrites nothing.

## What stays as written

Incremental tables, operations scripts, models with pre or post operations and models whose `${...}` code depends on the file are never rewritten, and the tables they read are kept unchanged. A model that uses a project variable as a value, for example `WHERE status = '${dataform.projectConfig.vars.status}'`, is also kept as written, because the prover cannot read the variable's value. Assertions on removed or rewritten tables are dropped and listed with the reason, unless you ask to keep them.

## Limits

The search is greedy and time-limited, so the result is small but not always the smallest. Proofs are only as sound as KumoSQL's prover. The full guide lists every option, how files are written back, and how assertions and `dependencies` are handled. The [evaluation guide](evals/project-reduction.md) reports how much it removes on generated and open-source projects.
