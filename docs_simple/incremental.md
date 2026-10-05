# Checking incremental models

[All simple guides](README.md) · [Full reference](../docs/incremental.md)

An incremental model updates an existing table instead of rebuilding it from scratch. The question is whether, after each allowed source change, that table matches a full rebuild.

## A small example

Suppose each run loads events newer than the largest timestamp already saved. That can work when events arrive in timestamp order. A late event with an older timestamp gets missed. An update or deletion may leave an old row in the target.

This is why the checker needs a **contract**: which changes the source is allowed to make.

| Contract used by the project scan | What it tests |
| --- | --- |
| `append_only` | New rows under its insert assumptions |
| `late_and_duplicate` | Late arrivals and redelivered rows |
| `mutable` | Updates and deletes as well |

The Python API can specify individual change kinds and which source tables change.

## How a run works

Dataform builds an incremental table in one of two ways. The first run, or a full refresh, builds the whole table from the query. Every later run takes the new query result and either appends it or, when the model names a `uniqueKey`, merges it: a row whose key is already in the table is replaced, and a row with a new key is added. A merge never deletes.

So each run turns the old table and the current sources into the next table, and the checker compares that with a full rebuild from the same sources. Dataform also runs the model's setup statements and the table statement together as one script, which is why a variable declared in a setup statement can be used by the query.

## A second example: re-running the whole query

Many models skip the "only new rows" filter. They rerun the whole query on every run and let the merge replace what is already there. That is correct as long as the key never repeats in the result, is never empty, and no key that was written later disappears from the result, because the merge would leave its old row behind. The checker proves this for the shapes it understands, using the contract, and says `diverges` with a replayable example when a key repeats or disappears.

Two more cases get their own answer. If the result depends on how ties are broken, for example "keep the latest row per customer" when two rows share the latest time, there is no single correct table, and the verdict is **nondeterministic**; it comes with a pair of row orders that give different results. A model whose merge carries an `updatePartitionFilter` cannot match an old row at all, so it duplicates that row on a plain re-run.

## Scan a project

Install DuckDB support from a checkout with `python -m pip install ".[execution]"`, then:

```sh
python -m kumosql incremental-report path/to/project
```

The scan checks incremental Dataform actions under the three contracts. By default it infers source columns and assumes an `id` key and timestamp-shaped columns. These are assumptions to review. Use `--source-schema` to supply real source information; see command help for the file format.

## Read the verdict

- **safe**: a proof rule applies under the stated contract and assumptions.
- **nondeterministic**: the full rebuild itself depends on how ties are broken, so there is nothing single to compare with. A witness shows two orders of the same rows giving different results.
- **diverges**: a tested change sequence makes the incremental table differ from a full rebuild or fail. The report includes a replayable example.
- **unknown**: no proof applies and the search found no difference.
- **unsupported** or **timeout**: the model could not be checked within the available support or time.

An unknown is not a safety guarantee. A safe verdict is only as good as the contract and the stated assumptions, for example that a declared source key really is unique and never empty.

Statements that only change permissions or table options (such as `GRANT`) do not change rows, so they are skipped. A statement that inserts, updates or deletes rows after the table is built is not modelled, and the model is reported as unsupported rather than guessed.

The simulator models appends and merges. A merge can fail if several source rows match one target row; NULL keys do not match and can be inserted repeatedly. It runs adapted SQL in DuckDB, so it only claims the BigQuery behavior explicitly modeled.

A project scan reports counts of verdicts for orientation. Source columns are guessed and nobody labelled the answers, so those counts are not a score. The scored cases and what they do and do not show are in the [full reference](../docs/incremental.md#scores): they are cases the authors wrote and labelled, and the rules were written while looking at most of them, so held-out cases are reported separately.

The full guide covers Dataform's run templates, the proof rules and their conditions, script variables, ties, the assumptions, watermark rules, lookback windows, deduplication, grouped summaries, unchanged joined tables, pre-operations, and adapted pg_ivm workloads. Always read the contract alongside the verdict.

<!-- Sections for later changes go here: reload windows, repairs, change-generator gaps. Not written yet. -->
