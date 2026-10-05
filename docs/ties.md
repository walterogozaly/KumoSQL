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

A window nobody reads (a column a reader never uses) is not a site, and neither is a top-level `ORDER BY` without `LIMIT`: the result is a bag of rows, so presentation order does not change it.

## Refuting on one database

The static pass above answers for every database that respects the declared keys. A refutation is about one database `D`, where a sharper question can be answered from the data: does the answer change with the way a tie is broken? `kumosql.tie_data` asks it by running a query on `D` under every tie-break it can name and keeping the distinct results (a *profile*):

- the table rows stored in every order (tables of up to four rows; larger tables get the stored order, reversed, rotated and a few shuffles; at most 48 databases per query), because DuckDB on one thread and SQLite break the ties of a window, a `LIMIT`, an `ARRAY_AGG` and a bare column by storage order;
- every `LIMIT`/`OFFSET`, nested ones included, run again with all its output columns added to its `ORDER BY`, ascending and descending, so the cut falls on another tied row (the same probe as the proof re-check's);
- every `ANY_VALUE`, `FIRST`, `LAST` pick made to fail when its group holds several values, since a pick follows hash order and storage order does not move it. A free pick makes the profile *incomplete*: the results BigQuery could return are not all observed.

A query is *determined* on `D` when its profile is complete and holds one result. `refutation_verdict` then allows exactly three outcomes for a difference on `D`:

| Left | Right | Verdict |
| --- | --- | --- |
| determined | determined | `different` (or `same` when they agree) |
| determined | complete, and none of its results equals the left's | `different` |
| anything else with a difference | | `unknown`: it may be which tied row each query kept |

`find_targeted_difference`, the executed search behind `prove_equivalent_algebraic(search_counterexample=True)` and the bounded checker's DuckDB and SQLite replays all apply the verdict, so a database where the difference is a tie artefact is skipped and the search goes on; a pair whose every difference is one stays unknown. `tie_aware=False` on `find_targeted_difference` restores the old repeat-only check, and `tie_artefacts=[]` collects the skipped databases with their reasons. The counterexample search of `kumosql.counterexample` already refused a difference that depends on a shuffle, a free pick or a tied cut, and is stricter than the verdict (both queries must be stable).

Limits: the profile lists the tie-breaks that were tried. Tables of up to four rows are covered exhaustively; on larger ones a tie-break that none of the sampled orders reaches can be missed, so `determined` is evidence, not proof. It relies on the engine breaking ties by storage order. Finding no difference never proves equivalence: the module only withholds a refutation, so it cannot move a proof. The static `tie_determinism` verdicts are not used here because they are about all legal databases; a `deterministic` verdict would only save runs.

## Checks

`tests/test_tie_refutation.py` runs pairs that differ only in a tie (a latest-row window against a grouped MIN join, a tied `LIMIT`, `ANY_VALUE`, `FIRST_VALUE`) and pairs that differ whatever the tie-break, through each refuter. `tests/test_tie_determinism.py` lists the verdicts for 50 queries without and with a declared key, and runs every query judged deterministic on DuckDB with its table's rows stored in every order (DuckDB on one thread breaks ties by storage order): each returns the same rows every time.
