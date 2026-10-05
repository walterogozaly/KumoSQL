# MATCH_RECOGNIZE, explained simply

[Full reference](../docs/match-recognize.md)

`MATCH_RECOGNIZE` is a BigQuery clause that finds a pattern in a sorted run of rows, for example "three days of falling prices in a row". It returns one row per match, with the columns you list in `MEASURES` and the ones you partition by.

```sql
SELECT * FROM prices MATCH_RECOGNIZE (
  PARTITION BY ticker ORDER BY day
  MEASURES FIRST(price) AS start_price, LAST(price) AS end_price
  PATTERN (DOWN{3,})
  DEFINE DOWN AS price < PREV(price))
```

This returns `ticker`, `start_price` and `end_price`. It does not return `day` or the other columns of `prices`.

## What KumoSQL does with it

- It reads every spelling BigQuery accepts, and refuses what BigQuery refuses (for example `ONE ROW PER MATCH`, which other databases have and BigQuery does not), so it never works from SQL that would not run.
- In a pipeline, the model reads `prices`, and it reads exactly the columns the clause names. The output columns are the partition columns and the measures, with the names BigQuery gives them. `start_price` traces to `price`; if a name cannot be worked out, the model is reported as untraceable instead of guessed.
- It never says two queries with this clause are equal, and no cleanup rule rewrites such a statement: it is left exactly as you wrote it, with a note saying so. Other statements in the same file are still cleaned up.

## Limits

- It is tested on small hand-written examples checked with a BigQuery dry run, not on a large collection of real queries.
- Lineage says which columns a result depends on, not which rows match.
- The details, the full list of refused forms and the recorded fixtures are in the [full reference](../docs/match-recognize.md).
