# Nested data (ARRAY, STRUCT, UNNEST) pairs

112 query pairs over ARRAY and STRUCT columns, scored by `tools/nested_data_bench.py` (results files
`nested-data-proof` and `nested-data-executed`; the page is `docs/evals/nested-data.md`).

* Source: none. Every pair, schema and stored database was written for this eval, in the idioms of GA4
  exports (`event_params` key and value lookups, `items`), Snowplow events (context arrays, `unstruct`
  structs) and a small shop schema (tags, order lines, scores), plus pairs over literal arrays. Table and
  column names are invented or follow public export schemas; nothing was copied from a project or a dataset.
* Licence: the same as the repository.

## Files

| file | content |
| --- | --- |
| `pairs.json` | `schemas` (schema set to table to column to BigQuery type), `datasets` (stored databases per schema set, rows as JSON), `pairs` |

A pair has `id`, `family` (`ga4`, `snowplow`, `shop`, `membership`, `array`, `struct`, `literal`), `schema`
(the schema set it runs over), `label`, `left`, `right`, `note`, `bigquery_differs`, and optionally `keys`
(per table, columns that are unique and never NULL: the pair is only checked on stored databases that respect
them) and `nondeterministic`.

## Labels and how they were checked

| label | meaning | pairs |
| --- | --- | --- |
| `equivalent` | the same rows (as a bag) on every database that respects BigQuery's storage rules | 65 |
| `different` | a trap: some database returns different rows | 47 |

The storage rules the labels assume: a stored array is never NULL (BigQuery writes a missing array as `[]`) and
holds no NULL element; struct equality is by position.

Each schema set has stored databases (GA4 3, Snowplow 2, shop 3) built to separate the traps: an empty array, a
repeated key, a NULL field, an empty table, a second element. `bigquery_differs` lists, for each pair, the
indexes of the stored databases on which BigQuery returned different rows for the two queries. It was filled in
by running each database as inline data on BigQuery (`python tools/nested_data_bench.py --bigquery-sql` prints
one query per database: both queries of every pair, rows written with `FORMAT('%T')`, compared as bags; no table
is read or stored):

* Every database of every schema set was run: ga4 0, 1 and 2, snowplow 0 and 1, shop 0, 1 and 2.
* Every trap but one differs on at least one database. No equivalent pair differs on any.
* The exception is `shop-array-subquery-unordered`, marked `nondeterministic`: `ARRAY(SELECT t FROM UNNEST(tags))`
  has no fixed order, so BigQuery may return the elements in the stored order or not. Its label rests on that
  argument, not on a database, and the scorer never counts it as wrong either way.
* A pair that BigQuery would fail on a stored database (a scalar subquery returning more than one row) is not
  checked on that database.

`python tools/nested_data_bench.py --check-labels` replays the same databases on DuckDB through KumoSQL's
BigQuery translation and checks that DuckDB sees exactly the differences recorded for BigQuery. Four pairs are
declined by the translation on every database, so they rest on the BigQuery run alone: the two struct-equality
pairs (`ga4-struct-equality-vs-fields`, `ga4-struct-equality-null-field`; DuckDB compares structs by field name),
`shop-array-length-filtered` and `shop-array-subquery-unordered`.

A label says two queries differ on a stored database, or that they agreed on all of them plus the argument in the
pair's `note`; it cannot show that two queries agree on every database. The traps are the pairs a person would
write wrongly by habit (a LEFT JOIN against a cross join, `COUNT(*)` against a count of distinct keys, a lookup
over a repeated key, `NOT IN` against `NOT EXISTS`), so a trap proved equal is the kind of wrong answer the eval
is built to catch.

## Held-out split

One pair in four is held out: those whose id, hashed as `sha1("nested-data\n" + id)`, is a multiple of 4 in
integer value (26 pairs). The split is by id, so it does not move when pairs are added. Development and rules
look at the other pairs only; the held-out score is measured once at the end of a piece of work.
