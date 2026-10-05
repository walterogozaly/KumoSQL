# Nested data: ARRAY, STRUCT and UNNEST

[Plain-language version](../../docs_simple/evals/nested-data.md)

112 hand-written query pairs over ARRAY and STRUCT columns: 65 are equivalent and 47 are traps (they look alike and differ). They are in the idioms of GA4 exports (`event_params` lookups of a key and a typed value, `items`), Snowplow events (context arrays, `unstruct` structs) and a small shop schema (tags, order lines, scores, ship-to structs), plus pairs over literal arrays. The pairs, the schemas and the stored databases are in `tests/fixtures/nested_data/pairs.json` (provenance and how the labels were checked: `tests/fixtures/nested_data/README.md`). Results files: `nested-data-proof` and `nested-data-executed`.

```
python tools/nested_data_bench.py                      # about 80 seconds
python tools/nested_data_bench.py --show unknown       # list the pairs that come out as unknown
python tools/nested_data_bench.py --check-labels       # replay the stored databases on DuckDB (about 10 seconds)
python tools/nested_data_bench.py --bigquery-sql       # one label-check query per stored database, for BigQuery
python tools/nested_data_bench.py --scan               # unlabelled coverage scan of nested queries in Spider 2.0 (dev) and bq_corpora
python tools/nested_data_bench.py --write-results
```

## What is scored

Each pair gets one outcome from `prove_equivalent_algebraic` (BigQuery dialect, the fixture's column types and keys, counterexample search on):

1. **proven**: the prover proved the two equivalent. A proof of a trap is wrong.
2. **refuted**: the prover, or a database the counterexample search built with ARRAY and STRUCT values and ran on DuckDB, shows the two differ. A refutation of an equivalent pair is wrong.
3. **unknown**: anything else, including an error. Unknown is never wrong.

Proofs and counterexamples are scored separately: proofs are counted over the 65 equivalent pairs, counterexamples over the 47 traps. One pair in four, by a hash of its id, is held out (26 pairs: 19 equivalent, 7 traps); development looks only at the others.

## Labels

A trap is labelled by running both queries on BigQuery over the stored databases (inline data, nothing stored): every trap but one returns different rows on at least one database, and no equivalent pair differs on any. The stored databases follow BigQuery's rules for stored arrays (never NULL, no NULL element), and they are built to separate the traps: an empty array, a repeated key, a NULL field, an empty table, a second element. `--check-labels` replays them on DuckDB through [the BigQuery translation](../bigquery-on-duckdb.md) and checks it sees the same differences as BigQuery did. Four pairs are declined by the translation (the two struct-equality pairs, `shop-array-length-filtered` and `shop-array-subquery-unordered`) and rest on the BigQuery run alone, and one trap, `shop-array-subquery-unordered`, depends on an unspecified order, so its label rests on the argument and no database. A label shows two queries differ on a stored database; it does not show that two queries agree on every database. See the fixture README for the details.

## Scores

2026-10-05, on master's provers plus this change (the data, the translation and the counterexample search; no prover rule for nested data yet), 0 wrong in both:

| | all | development | held out |
| --- | --- | --- | --- |
| equivalent pairs proved | 6/65 | 4/46 | 2/19 |
| traps refuted | 33/47 | 28/40 | 5/7 |

Before this change a query over an array column was outside the counterexample search (only `CROSS JOIN UNNEST` of a literal array was allowed) and no counterexample database held an array or a struct. The numbers before it are not recorded for this eval, since it is new here. The development proofs are three struct field reads (`device.category` against `e.device.category`, a Snowplow struct field under an alias, the shop `ship.city`) and a filter on an UNNEST element written with a comma join and with `CROSS JOIN` and the equality flipped. Every other equivalent pair is unknown: the prover has no rule yet for `IN UNNEST`, a correlated scalar subquery over an UNNEST or `ARRAY_LENGTH`, which are the next change.

Development outcomes by family (equivalent pairs proved, traps refuted):

| family | equivalent | traps |
| --- | --- | --- |
| GA4 (`event_params`, `items`, `device`) | 1/17 proved | 13/16 refuted |
| Snowplow (contexts, `unstruct`) | 1/5 proved | 5/5 refuted |
| shop (tags, lines, scores, ship) | 1/6 proved | 5/5 refuted |
| struct fields | 1/4 proved | 1/1 refuted |
| array operations | 0/5 proved | 2/5 refuted |
| literal arrays | 0/6 proved | 1/6 refuted |
| membership (`IN UNNEST`) | 0/3 proved | 1/2 refuted |

The 12 development traps that are not refuted fall into four kinds: a whole-struct comparison (the search refuses it), `ARRAY(SELECT ..)` (outside its allow-list), `ARRAY_REVERSE`, a `LIMIT` inside a subquery or a `CAST` of an array element (outside it too), and queries over literal arrays alone (`[10, 20, 30][OFFSET(1)]` against `[ORDINAL(1)]` reads no table, so there is no database to build and the prover has to decide them).

## What the search can now do

A trap needs a database with the right shape. The search builds arrays and structs the way BigQuery stores them, and finds these (each is a listed regression in `tests/test_nested_data_bench.py`):

- **A repeated key.** `(SELECT MAX(value.int_value) FROM UNNEST(event_params) WHERE key = 'ga_session_id')` against `LEFT JOIN UNNEST(event_params) AS p ON p.key = 'ga_session_id'` agree while every key appears once; a row with the key twice gives one row on the left and two on the right.
- **An empty array.** `arr[SAFE_OFFSET(0)]` against `UNNEST(arr) WITH OFFSET AS o WHERE o = 0` as a cross join: an empty array gives a row with NULL on the left and no row on the right.
- **A one-element array.** `ARRAY_LENGTH(x) > 1` against `EXISTS (SELECT 1 FROM UNNEST(x))`.

and correctly does not refute `NOT 3 IN UNNEST(arr)` against `NOT EXISTS (SELECT 1 FROM UNNEST(arr) AS s WHERE s = 3)`: they differ only if an element is NULL, and a stored array has none.

Not refuted and not proved yet: `IN UNNEST` (the prover has no rule), correlated scalar subqueries over an UNNEST and `ARRAY_LENGTH > 0` against `EXISTS`. A result that depends on the row order of an UNNEST is not caught by the reversed-rows stability check; the guard is that the search refuses windows over an UNNEST and keeps `ARRAY_AGG` and `ARRAY(subquery)` out of its allow-list.

## Limits

- The 112 pairs were written by the person who changed the counterexample search and the translation, for this eval, and fixed before any prover rule for nested data was written; a quarter is held out by a hash of the id, but this is a regression and honesty check on one author's pairs, not an independent benchmark. Rules for nested data should be developed on the development pairs only and the held-out score measured once.
- Labels come from BigQuery runs on a few stored databases. They separate the traps but do not prove an equivalent pair equivalent; the argument is in each pair's `note`.
- A refutation is a DuckDB run after translation, so it is only as faithful as [the translation](../bigquery-on-duckdb.md); the search runs only when the translation follows BigQuery or fails where BigQuery fails, and it refuses whole-struct comparisons.
- `--scan` is an unlabelled coverage scan (a query checked against itself): it says whether the prover could encode a nested query from Spider 2.0's development split or from the `bq_corpora` projects, not whether it is right.
