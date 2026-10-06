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

## Witnesses

`unknown` says the result may depend on ties; a *tie witness* shows it. `kumosql.tie_witness.find_tie_witness` looks for a small database and two physical orders of its rows on which DuckDB (one thread, BigQuery SQL through `bigquery_on_duckdb`) returns two different bags. DuckDB with `SET threads=1` breaks ties in windows, `LIMIT` and `ARRAY_AGG` by storage order, so the difference replays.

```python
from kumosql.result_equivalence import DataRules
from kumosql.tie_witness import find_tie_witness, replay

schema = {"events": {"id": "INT64", "user_id": "INT64", "ts": "TIMESTAMP", "value": "INT64"}}
rules = {"events": DataRules(frozenset({"id"}), (("id",),))}  # id is a NOT NULL key
witness = find_tie_witness(
    "SELECT user_id, ts, value FROM events "
    "QUALIFY ROW_NUMBER() OVER (PARTITION BY user_id ORDER BY ts DESC) = 1",
    schema, rules,
)
witness["tables"]    # {"events": [two rows of one user with the same ts, different value]}
witness["orders"]    # {"first": {"events": [0, 1]}, "second": {"events": [1, 0]}}
witness["results"]   # the two different result bags
replay(witness)      # True: rebuilds the database from the JSON and checks it again
```

The search runs the query on the databases of `targeted_data.database_suite` built around it, plus variants whose rows agree on every non-key column but one (equal sort keys, different payload). On the first database where a storage order from `refute._orders` changes the result, it drops rows greedily while the result still depends on the order, which usually leaves two tied rows. Every database keeps the declared NOT NULL columns, keys and foreign keys (`refute.legal`), so a witness never contradicts a fact the analysis rested on. Each order is then run twice and once more with DuckDB's optimizer off (`duckdb_load.run_unoptimized`); a difference that does not repeat, or that the unoptimized run does not show, is dropped, so a random function or an engine bug cannot make a witness. `replay` repeats all of these checks on a stored witness (and rejects one whose rows break a recorded key or NOT NULL column).

A witness belongs to the whole query: it does not say which of the query's sites caused the difference. No witness does not mean the query is deterministic: the search is bounded and tries a few dozen small databases. The tests check the other direction on a list of queries: none the analysis calls `deterministic` gets a witness.

## Lint over a Dataform project

`python -m kumosql ties PROJECT` (`kumosql-ties`) reads every query model of a Dataform project, runs `analyze` on it and, for each model with an `unknown` site, `find_tie_witness`.

```
python -m kumosql ties path/to/project [--budget 10] [--limit N] [--json]
```

- A **finding** is a model with an `unknown` site and a witness that replays (`tie_witness.replay`). The output names the model, the first site, the suggested fix and the size of the witness; `--json` includes every site and the witness itself.
- An `unknown` site without an attributable witness (no database found within `--budget`, unsupported inputs, or an upstream model also has an unknown tie site) is listed as *unwitnessed*. It is not reported as a finding.
- Incremental models and models whose query still holds Dataform expressions the loader could not resolve are skipped, with the reason (`--json` lists them): an incremental table's rows are not its query's output.

The lint safely inlines upstream table and view queries before searching, so the witness is built from
raw source rows that can flow through upstream filters and projections. If an upstream model has incremental
state, operations, unresolved expressions, a cycle, or another unsupported shape, its dependent site stays
unwitnessed. A witness that could be caused by an upstream unknown tie is also left unwitnessed. Declared
keys and NOT NULL facts on raw inputs are kept. Types come from project schemas when available and are
otherwise guessed from column names. A finding with several unknown sites lists them all; the witness does
not identify which one it exercises.

`tools/tie_lint_check.py` independently checks each finding against inlined upstream table and view models.
It reports an unconfirmed result as a bounded-search false alarm candidate, not as proof that the finding is
false; it labels findings ambiguous when an upstream model also has an unknown tie site.

## Measured on the fixture

On the generated fixture (`python tools/make_dataform_fixture.py OUT --models 3000 --seed 11`, 3,378 models), `python tools/tie_lint_check.py OUT --budget 0.2 --check-budget 5` completed in 128 seconds: 1,260 sites, 175 replayable findings, 600 unwitnessed sites, all 175 findings confirmed from source rows, 0 ambiguous findings, 0 false alarm candidates and 0 inconclusive checks. Sites whose witness might come from an upstream unknown tie are included among the unwitnessed sites.

The loaded project in the app has the same lint as a background job: `POST /api/ties/run` (optional `budget`, `limit`) starts it, `GET /api/ties` returns its state (`idle`, `running`, `done` with the result, `cancelled`, `error`) and `POST /api/ties/cancel` stops it. There is no page for it yet.

## Checks

`tests/test_tie_determinism.py` lists the verdicts for 50 queries without and with a declared key, and runs every query judged deterministic on DuckDB with its table's rows stored in every order (DuckDB on one thread breaks ties by storage order): each returns the same rows every time.
