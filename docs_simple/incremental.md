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

## Scan a project

Install DuckDB support from a checkout with `python -m pip install ".[execution]"`, then:

```sh
python -m kumosql incremental-report path/to/project
```

The scan checks incremental Dataform actions under the three contracts. By default it infers source columns and assumes an `id` key and timestamp-shaped columns. These are assumptions to review. Use `--source-schema` to supply real source information; see command help for the file format.

## Read the verdict

- **safe**: a proof rule applies under the stated contract and assumptions.
- **diverges**: a tested change sequence makes the incremental table differ from a full rebuild or fail. The report includes a replayable example.
- **unknown**: no proof applies and the search found no difference.
- **unsupported** or **timeout**: the model could not be checked within the available support or time.

An unknown is not a safety guarantee.

The simulator models appends and merges. A merge can fail if several source rows match one target row; NULL keys do not match and can be inserted repeatedly. It runs adapted SQL in DuckDB, so it only claims the BigQuery behavior explicitly modeled.

The full guide covers watermark rules, lookback windows, deduplication, grouped summaries, unchanged joined tables, pre-operations, and adapted pg_ivm workloads. Always read the contract alongside the verdict.

## Delete-then-reload windows

Some models protect against late rows by deleting the newest hours of the target table before every run and then re-reading everything after what is left. For example, delete the last two hours, then load all source rows newer than the table's new maximum. This is safe when new rows only arrive at or after the source's newest time, and it is unsafe when a row arrives later than the window, is delivered twice, or is updated after being loaded. The checker proves the safe versions and keeps the others as refuted or unproven. A model that appends and reloads with `>=` repeats the rows at the new maximum, so it is refused. The proof assumes event times are never NULL and always later than the fallback date in the `COALESCE`. The details, including the variable form (`DECLARE w ...`), are in the [full guide](../docs/incremental.md#r8-delete-then-reload-windows).
