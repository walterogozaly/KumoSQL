# Ties and nondeterministic results

A window's `ORDER BY` sorts each partition into groups of tied rows. BigQuery leaves the order inside such a group unspecified, and without `ORDER BY` the whole partition is one group. So `QUALIFY ROW_NUMBER() OVER (PARTITION BY user_id ORDER BY ts DESC) = 1` keeps *some* latest row per user: when two rows share the latest `ts`, which one comes back can change from run to run. The same goes for the rows an `ORDER BY .. LIMIT` keeps, for `LIMIT` without `ORDER BY`, and for aggregates that pick or collect rows in an order.

`kumosql.tie_determinism` finds every place in a query where that can happen and says whether the result depends on it. It is a pass over the syntax tree (no solver, no data), cheap enough to run on every model.

```python
from kumosql.smt_equivalence import TableConstraints
from kumosql.tie_determinism import analyze, tie_dependence

constraints = {"events": TableConstraints(not_null=frozenset({"id"}), keys=(("id",),))}
report = analyze(
    "SELECT user_id, ts, value FROM events "
    "QUALIFY ROW_NUMBER() OVER (PARTITION BY user_id ORDER BY ts DESC) = 1",
    constraints=constraints,
)
site = report.sites[0]
site.verdict  # "unknown": two rows of one user may share the latest ts
site.fix      # "add events.id (unique with the existing keys) to the window's ORDER BY"

# the short form: the reasons a result can depend on ties, given unique NOT NULL keys
tie_dependence("SELECT a FROM t ORDER BY b LIMIT 1", keys={"t": [("b",)]})  # []
```

`schema` and `constraints` have the shapes the provers take (`ProverSchema.columns` and `.constraints` fill them from the BigQuery catalog and Dataform assertions). Without a schema, the columns a query reads stand in for each table's columns.

## Sites

| Kind | Where | Depends on ties unless |
| --- | --- | --- |
| `window` | `ROW_NUMBER`, `NTILE`, `LAG`/`LEAD`, `FIRST_VALUE`/`LAST_VALUE`/`NTH_VALUE`, `ANY_VALUE`, and an aggregate over a `ROWS` frame | the rows cannot tie, or tied rows look the same (below) |
| `limit` | `ORDER BY .. LIMIT`/`OFFSET`, `LIMIT` without `ORDER BY` | the cut cannot fall inside a group of different rows |
| `aggregate` | `ANY_VALUE`, `MAX_BY`/`MIN_BY`, `ARRAY_AGG`/`STRING_AGG`/`ARRAY_CONCAT_AGG` with or without their own `ORDER BY` and `LIMIT` | each group's rows cannot tie, or tied rows give the same value |
| `array` | `ARRAY(SELECT ..)` | the subquery's order is total, or returns at most one row |

`RANK`, `DENSE_RANK`, `PERCENT_RANK`, `CUME_DIST`, and an aggregate over a whole partition or a `RANGE` frame (the default with an `ORDER BY`) give tied rows the same value, so they are listed as `deterministic` whatever the data. An analytic `ARRAY_AGG` or `STRING_AGG` is always `unknown`: BigQuery does not say in which order it collects.

## Verdicts

A site is `deterministic` for one of these reasons:

- **No two rows tie.** The `PARTITION BY` and `ORDER BY` expressions cover a unique key of the window's input: a declared key, `GROUP BY` keys, a `DISTINCT`, or anything else [output properties](output-properties.md) can show unique. For a `LIMIT`, the `ORDER BY` covers a key of the rows before the cut. For an aggregate, the `GROUP BY` keys plus the aggregate's own ordering do. `facts` lists the declared facts the verdict rests on, such as `(id) is unique in events`.
- **Tied rows look the same.** Every column read after the window (the outputs a reader uses, `QUALIFY`, the window's arguments, other windows) is a `PARTITION BY` or `ORDER BY` column, equal to one, or fixed by the `WHERE` clause. `SELECT user_id, ts FROM events QUALIFY ROW_NUMBER() OVER (PARTITION BY user_id ORDER BY ts DESC) = 1` returns the same rows whichever tied row is kept. Outputs nobody reads do not count: a `COUNT(*)` over the deduplicated rows only sees how many there are. Rows that are exact duplicates never count as a tie.
- **There is at most one row.** A `LIMIT 1` over a global aggregate keeps it.
- **Only existence is asked.** `EXISTS (SELECT .. LIMIT 1)` is true or false whatever rows are kept.

Every other site is `unknown`, with `fix` saying what would pin it down: a known unique key to add to the `ORDER BY` when there is one, otherwise columns unique within each partition (or keeping every tied row with `RANK`). `unknown` does not prove the result changes; it says no reason was found that it can't.

A name that is both a `SELECT` alias and a column (`SELECT value AS ts .. ORDER BY ts`) is read the way the query can mean it. An `OVER` clause and an aggregate's arguments only see the table's columns. In `ORDER BY`, `GROUP BY` and `QUALIFY` the name could be the alias or the column, so a verdict that depends on which one is `unknown`; without a schema a name counts as a column when the query reads it where aliases are not visible, or when a declared key or NOT NULL column names it. A `GROUP BY` name whose alias is an aggregate is always the column.

A window nobody reads (a column a reader never uses) is not a site, and neither is a top-level `ORDER BY` without `LIMIT`: the result is a bag of rows, so presentation order does not change it.

## Fuzzing the verdicts

`tools/tie_fuzz.py` hunts for a `deterministic` verdict that is wrong. It generates random windows (every ranking, navigation and aggregate function, with and without `PARTITION BY`, `ORDER BY` and a frame, `QUALIFY`), `ORDER BY .. LIMIT/OFFSET` (plain, `DISTINCT`, grouped, `UNION`, nested), `ANY_VALUE`/`MAX_BY`/`ARRAY_AGG`/`STRING_AGG` aggregates, `ARRAY(SELECT ..)` and scalar subqueries, alone or read by an outer query (a filter on the rank, a join, `SELECT *`), over `events(id, user_id, ts, value)` or over a derived table that keeps, loses or never had a key (self joins, `UNION ALL`, `GROUP BY`, `ROLLUP`, `UNNEST`). Some select items take another column's name (`value AS ts`) and some `ORDER BY` items are expressions that tie rows with different columns (`COALESCE(ts, 0)`, `MOD(ts, 2)`, `ABS(ts)`). It does this for three schemas: `id` a declared key, no constraints, and `(user_id, ts)` a declared composite key.

Every query `analyze` calls deterministic runs on DuckDB (one thread, `bigquery_on_duckdb.configure`) over six random databases of 2 to 4 rows with 2 or 3 values per column, respecting the declared facts, with the rows stored in every permutation (`result_equivalence.DatasetRunner`, rows swapped per permutation). The result bag must never change; each change is printed as it happens. A fifth of the `unknown` queries run the same way as a control, to show that the fuzzer sees tie dependence when it is there.

```
python tools/tie_fuzz.py --count 4000 --seed 1 --json out.json   # query i of a seed is Random("seed:i"); --start i reruns one
```

Measured on master at 04af1a9e (2026-10-05, seed 1, 7,500 queries in three schemas, aliases never shadowing a column): of 22,500 analyses 9,466 were deterministic, 8,689 of them ran (the rest use something DuckDB cannot run or translates with a part dropped, such as `LIMIT` inside `ARRAY_AGG`), and **none changed with the row order** (5,750 window sites, 2,485 `LIMIT` sites, 1,723 aggregate sites and 135 `ARRAY` sites among them; 1,107 are queries with no site at all). Of 2,292 `unknown` queries run as the control, 945 (41%) changed. That is the evidence behind the "no deterministic verdict varied" claim of the pull request that added the analysis (#477): it holds for queries without aliases that shadow a column.

With select aliases that shadow columns (seed 21, 1,000 queries in three schemas), master had **13 unsound verdicts in 1,212 deterministic runs**, all from taking an alias for the column of the same name when no schema is given (for example `SELECT value AS ts, ts AS value .. OVER (.. ORDER BY ts)`); with the fix (#684) there were none in 1,155 (89 fewer verdicts are `deterministic`: the ambiguous ones).

Limits: the tie breaking checked is DuckDB's (storage order on one thread), not BigQuery's; databases are tiny and hold only integers, so floating-point `SUM`/`AVG` (which BigQuery does not promise to compute in a fixed order) and string collations are not exercised; a clean run bounds nothing outside the generated shapes. `tests/test_tie_fuzz.py` runs 90 seeded queries in three schemas (about 30 seconds), and the same with shadowing aliases once #684 is merged (it skips itself until then).

## Checks

`tests/test_tie_determinism.py` lists the verdicts for 50 queries without and with a declared key, replays queries whose alias shadows a column on DuckDB (the traps must be `unknown`, the harmless ones stay `deterministic`), and runs every query judged deterministic on DuckDB with its table's rows stored in every order (DuckDB on one thread breaks ties by storage order): each returns the same rows every time.
