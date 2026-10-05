# Lists and records in BigQuery tables

[Simple eval index](README.md) · [Full reference](../../docs/evals/nested-data.md)

A BigQuery column can hold a list (an `ARRAY`) or a record (a `STRUCT`) instead of one value. Google Analytics 4 exports work this way: every event row carries a list of `event_params`, each a key with a value, and you read one with `(SELECT value.int_value FROM UNNEST(event_params) WHERE key = 'ga_session_id')`. `UNNEST` turns a list into rows, so a join against it can repeat or drop the outer row, and that is where lookalike queries come apart.

This eval is a set of 112 hand-written pairs of queries over such columns, in the style of GA4, Snowplow events and a small shop. 65 pairs mean the same thing written two ways, and 47 are traps: they look alike but give different rows on some data. A good checker proves the first kind and never proves the second.

## A concrete example

`SELECT e.event_name, (SELECT MAX(value.int_value) FROM UNNEST(e.event_params) WHERE key = 'ga_session_id') FROM events AS e` and the same with `LEFT JOIN UNNEST(e.event_params) AS p ON p.key = 'ga_session_id'` give the same rows while every key appears once per event. If an event has the key twice, the first returns one row with the largest value and the second returns two rows. KumoSQL finds this by building a small database with a repeated key, running both queries, and showing the rows that differ.

Another: `ARRAY_LENGTH(x) > 1` and `EXISTS (SELECT 1 FROM UNNEST(x))` agree except on an array with exactly one element. And `NOT 3 IN UNNEST(arr)` against `NOT EXISTS (...)` look risky because of NULL, but BigQuery never stores a NULL inside an array, so KumoSQL correctly does not call them different.

## What is scored

For each pair KumoSQL says one of three things: proven the same, shown different (by its own reasoning or by a database it built with lists and records and ran), or unknown. Proving a trap, or showing an equivalent pair different, is a wrong answer and must not happen. Unknown is allowed. Today the score is low on proofs and fair on finding the differences, and nothing is wrong. The reason is that the provers have no rules yet for `IN UNNEST` or for a lookup over an `UNNEST`; the numbers are in the [full reference](../../docs/evals/nested-data.md) and the README scoreboard.

## How the labels were checked

Every trap was run on BigQuery over a few small stored databases (inline data, nothing stored) and returns different rows on at least one of them; no equivalent pair differs on any. The same databases are replayed on DuckDB, through the BigQuery translation, as a second check. One trap depends on an order BigQuery does not promise, so its label is an argument, not a run.

## What to keep in mind

- The pairs were written by the person who made the counterexample search handle lists and records. A quarter is held out by a hash of the pair's name, but this is a regression and honesty check, not an independent test.
- A label shows two queries differ on some stored data; it cannot show that two queries agree on all data. For equivalent pairs the argument is in the pair's note.
- Finding no difference does not prove two queries equal, and a difference found on DuckDB is only as good as the translation to BigQuery's rules ([how that works](../bigquery-on-duckdb.md)).

```sh
python tools/nested_data_bench.py
```
