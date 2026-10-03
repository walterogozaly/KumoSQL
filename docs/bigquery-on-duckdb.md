# Running BigQuery SQL on DuckDB

Every refutation KumoSQL reports from execution runs BigQuery SQL on DuckDB after sqlglot translates it: the counterexample searches (`kumosql.executed_refutation`, `kumosql.refute`, `kumosql.counterexample`), the bounded checker's replay, random databases (`kumosql.random_check`), synthetic-data comparison (`kumosql.result_equivalence`) and incremental-model simulation. Where the two engines read the same SQL differently, a DuckDB difference can be a false BigQuery refutation, which is a wrong answer. `src/kumosql/bigquery_on_duckdb.py` closes those gaps for every BigQuery-dialect run; SQL read in other dialects (MySQL, Postgres, SQLite, DuckDB evals) is run as before.

It does three things.

**Session settings** (`configure(connection)`): `NULL` sorts first ascending and last descending, as in BigQuery, and timestamps are read in UTC whatever the machine's zone.

**Translation fixes** (`faithful(tree)`), each checked against BigQuery itself:

| BigQuery | DuckDB before | Now |
| --- | --- | --- |
| `NUMERIC` keeps 9 decimal digits | sqlglot writes `DECIMAL`, which DuckDB reads as `DECIMAL(18, 3)`: `CAST(0.0001 AS NUMERIC)` was 0 | `DECIMAL(38, 9)` |
| `EXTRACT(DAYOFWEEK)` is 1 for Sunday | 0 for Sunday | plus one |
| `EXTRACT(WEEK)` counts Sunday weeks (week 0 before the first Sunday) | ISO weeks: 2016-01-01 is 53 | `strftime(.., '%U')`, `WEEK(MONDAY)` is `%W` |
| `DATE_TRUNC(.., WEEK)` and `DATE_DIFF(.., WEEK)` start weeks on Sunday | sqlglot 26 writes Monday weeks | built from Monday weeks shifted a day |
| `SUBSTR(s, 0, n)` and a position before the start read from position 1 | `SUBSTR('abc', 0, 2)` is `'a'` | position 1 |
| `REGEXP_EXTRACT` without a match is `NULL` | `''` | `NULL` |
| `CONCAT`, `LEAST`, `GREATEST` are `NULL` when an argument is | sqlglot 26 writes DuckDB's, which skip NULLs | `NULL` |
| `CAST(NUMERIC AS STRING)` drops trailing zeros (`'1.5'`) | `'1.500000000'` | trailing zeros dropped |
| `DATE_TRUNC`, `DATE_ADD` on a date return a date | a midnight timestamp | read back as a date |
| a NULL array is returned as `[]`; structs compare by position | `NULL`; structs compare by field name | read that way |

**Guards.** Where BigQuery fails, or where no faithful translation exists, the DuckDB run fails too, so the pair stays unknown instead of being refuted: division, `MOD` and `DIV` by zero; `SUM` past `INT64`; `POW`/`EXP` overflow; `NUMERIC` products and quotients (BigQuery rounds them to 9 digits, DuckDB keeps 18 or switches to `DOUBLE`); `>>` of a negative number (a logical shift in BigQuery); `CAST(FLOAT64 AS STRING)` (`'2'` against `'2.0'`); strings cast to numbers, booleans, dates or timestamps in any form the engines may read differently (`'1.0'` or `'1e3'` as `INT64`, `'t'` as `BOOL`, `'nan'` and `'inf'`); a result holding an array with a `NULL` element or a non-finite float. A whole query is refused when it uses `FORMAT`, `COLLATE`, approximate aggregates, `UNNEST .. WITH OFFSET` (sqlglot writes a 1-based `WITH ORDINALITY`), `BIGNUMERIC`, `WEEK(<weekday>)` other than Sunday or Monday, a `STRUCT` that is compared, grouped or read whole, or a float literal large enough to overflow.

As a result DuckDB never holds a NaN or an infinity during a BigQuery run, so the NaN rules (NaN equal to itself and above every number in DuckDB, unequal and first in BigQuery) cannot decide an answer.

A database on which a guard fires is one BigQuery fails on: the searches skip it and try the next one (`is_bigquery_failure(error)`), and `check_result_equivalence` does not count it. One consequence: a pair told apart only on a database with a zero divisor is no longer refuted there, since BigQuery shows an error on it, not a difference.

Not acted on: comparisons BigQuery rejects at compile time (`1 = TRUE`, `1 = '1.1'`, `ARRAY` equality or ordering, nested arrays). BigQuery has no result for such a query, so there is nothing for a DuckDB difference to contradict. `CAST(TIMESTAMP AS STRING)` with fractional seconds and decimal literals (exact in DuckDB, `FLOAT64` in BigQuery) are also left as they were; the second matches how the provers read literals.

`tests/test_bigquery_on_duckdb.py` keeps one test per divergence, with the values BigQuery returned, and pairs that BigQuery finds equal but the searches used to refute. The divergence list started from an outside catalogue (35 rows) and was checked row by row in DuckDB 1.5.6 and BigQuery; the table above includes several divergences that catalogue missed.
