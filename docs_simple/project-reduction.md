# Shrinking a Dataform project to the outputs you need

[All simple guides](README.md) · [Full reference](../docs/project-reduction.md)

Name the tables of a Dataform project that people actually use. KumoSQL proposes a smaller project that still builds each of them under the same name, with the same columns in the same order and the same rows. It returns a patch for review; it changes your files only if you ask it to.

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

Incremental tables, operations scripts, models with pre or post operations and models whose `${...}` code depends on the file are never rewritten, and the tables they read are kept unchanged. Assertions on removed or rewritten tables are dropped and listed with the reason, unless you ask to keep them.

## Project variables

A model that compares a column with a project variable, for example `WHERE status = '${dataform.projectConfig.vars.status}'`, can be folded and rewritten like any other. KumoSQL treats the variable as a value it does not know: the same wherever you write the same variable, and never equal to a plain word you typed, even the one the variable holds today, because a run can set it to something else. So `status = <variable> AND status = 'paid'` is not called empty, and a table that filters on the variable is not merged with one that filters on `'paid'`. The variable is written back exactly as you wrote it, quotes included.

The proof assumes the variable holds one plain string, with no quote or SQL code in it. A variable inside a longer string (`"pre_${...}"`), or in a raw or triple-quoted string, is still kept as written, and the reason is listed.

## Limits

The search is greedy and time-limited, so the result is small but not always the smallest. Proofs are only as sound as KumoSQL's prover. The full guide lists every option, how files are written back, and how assertions and `dependencies` are handled. The [evaluation guide](evals/project-reduction.md) reports how much it removes on generated and open-source projects.
