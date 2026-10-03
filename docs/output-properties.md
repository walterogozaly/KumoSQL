# Output properties

`kumosql.output_properties.infer_properties(sql, constraints, schema, dialect="bigquery")` says, without running the query, which facts about its output are guaranteed. It is a deterministic pass over the syntax tree (no solver, no LLM), so it is cheap enough to call for every model. Anything it cannot establish is left out: "not known" is always a safe answer.

```python
from kumosql.output_properties import infer_properties
from kumosql.smt_equivalence import TableConstraints

schema = {"orders": ["id", "customer_id", "amount"]}
constraints = {"orders": TableConstraints(not_null=frozenset({"id", "customer_id"}), keys=(("id",),))}
props = infer_properties("SELECT customer_id, COUNT(*) AS n, SUM(amount) AS s FROM orders GROUP BY customer_id", constraints, schema)
props.non_null("n")                  # True: COUNT is never NULL
props.non_null("s")                  # False: amount may be NULL, so a group's sum may be
props.is_unique("customer_id")       # True: one row per group
props.scalar_subquery                # "unknown": it can return several rows
props.assumptions_for("customer_id") # ("orders.customer_id is NOT NULL",)
```

`constraints` and `schema` have the shapes the prover takes (`ProverSchema.constraints` / `.columns` fill them from the BigQuery catalog and Dataform assertions). Without them only facts that follow from the query itself are found.

## What it infers

| Fact | Rule |
| --- | --- |
| Column never NULL | Literals, `COUNT`, `ROW_NUMBER`, `IS NULL`; declared NOT NULL columns read through inner joins; `COALESCE` with a non-NULL argument; arithmetic, comparison, `CAST`, `CASE` with an `ELSE`, and null-propagating functions whose arguments are all non-NULL (every argument, so `SUBSTR`'s start and length or `ROUND`'s decimals count too); `SUM`/`MIN`/`MAX`/`AVG` of non-NULL values under a `GROUP BY` (a group is never empty), but not in a global aggregate (an empty table gives NULL); a column the `WHERE` clause (or an inner join's `ON`) tests with `IS NOT NULL`, a comparison, `BETWEEN`, `LIKE` or `IN`, through `AND` and `OR` (an `OR` needs both sides) |
| Outer joins | The nullable side's columns become nullable again, even ones a subquery had filtered to non-NULL. A later `WHERE` that needs a column of that side to match makes the join an inner join for its rows |
| Unique columns | `GROUP BY` keys (when all are projected, by name, ordinal or alias), `DISTINCT` (the whole row), `UNION`/`INTERSECT`/`EXCEPT` distinct, declared keys of a table read through a join that matches each of its rows at most once (the other side matched on its own key, or on constants and outer references), a pair of keys across a join that can fan out (across a `FULL JOIN` only when one of the key columns is never NULL on its own side, since a row padded on the left and one padded on the right are otherwise identical when both keys are NULL or empty), keys carried by an inner-join or `WHERE` equality |
| Row count | A global aggregate returns exactly one row, `LIMIT 1` and `SELECT` without `FROM` at most/exactly one, a lookup by every column of a key (constants, parameters, or outer columns of a correlated subquery) at most one |
| Grouping sets | `ROLLUP`, `CUBE`, `GROUPING SETS` and MySQL's `WITH ROLLUP`: a column grouped in some sets but not all is nullable; `SUM` and friends are nullable when a set is `()` (it sees an empty table); `GROUPING(...)` is never NULL. All grouping columns together are unique when every grouped value is non-NULL in the input and no set repeats (the NULLs then tell the sets apart) |
| `VALUES` | Read as literal rows: a column is non-NULL when no cell is NULL, one row is exactly one row, and a column (or the whole row) is unique when its literals are of one kind and pairwise different (strings compared ignoring case, long numbers not compared) |
| `LATERAL` | A lateral subquery is analysed with its references to earlier FROM items as per-row values, so a lookup by key or an aggregate gives at most one row per left row and keeps the left side's keys |
| Names | A repeated output name (`SELECT a.id, b.id`, or `*` over a join) keeps its facts by position: `UniqueKey.positions` lists the columns by output position, and `props.column(name)` returns None when the name is ambiguous. Column alias lists (`AS t(a, b)`, `WITH c(a, b)`), unaliased subqueries and parenthesised joins in FROM are followed. `* EXCEPT (...)` leaves its columns out and `* REPLACE (expr AS c)` analyses `c` as `expr`; `* RENAME` and `* ILIKE` are unsupported |
| Scalar subquery | `props.scalar_subquery` is `exactly_one`, `at_most_one` or `unknown` (may return several rows) |

Uniqueness means no two rows are identical with NULL equal to NULL, the way `GROUP BY` and `DISTINCT` compare. Facts that rest on a declared NOT NULL column or key carry it in `assumptions` (`"(id) is unique in orders"`), the same declared facts the prover's results list; facts that follow from the query alone carry none. Unsupported shapes (unknown tables, `UNNEST` in FROM or as a set-returning select item such as DuckDB's `SELECT UNNEST(list)`, `USING`, recursive `WITH`, `GROUP BY ALL`) return `OutputProperties(unsupported=...)`.

The prover uses it for one thing today: a proof no longer lists "scalar subqueries return at most one row" as an assumption when every shared scalar subquery is shown to return at most one row.

## Evaluation

`python tools/output_properties_bench.py` (development cases), `--held-out` (final evaluation only) and `--adapted` (SQLSolver's queries). The source, labels and checking are described in the tool's docstring; results are in `benchmarks/results/output-properties*.json` and the README scoreboard.

* **Original cases** (`tests/fixtures/output_properties/cases.json`, 108 queries, 198 labelled claims on two schemas (shop and warehouse)): written from the SQL semantics, including the negative cases (a join destroying uniqueness, `SUM` over an empty table, an outer join undoing a filter's guarantee, a correlated subquery that can return several rows). Every label is checked against executed data: a true label may never be violated on random databases that respect the declarations, and a false label must be violated by some database (69 of 69 are). The analysis is scored `proved` (a true claim found), `unknown` (a true claim missed), `correct_unknown` (a false claim not asserted) and `wrong` (asserted but violated, must stay 0). There is no timeout.
* **Held-out cases** (`held_out.json`, 20 queries, 35 claims on a third schema): run once after the analysis was frozen. The first held-out set (32 queries) showed two misses (`CASE`/`IF` guarded by `IS NULL`, `ROW_NUMBER` uniqueness); those were fixed and the set moved into the development cases, so this one is fresh.
* **Adapted queries**: the queries of SQLSolver's Calcite, Spark, TPC-H and TPC-C pairs (pinned in `tests/fixtures/sqlsolver`), analysed with their schemas; there are no labels, so every fact the analysis asserts is checked against executed rows. All 761 queries are supported; the analysis asserts 2,669 facts and none is violated in 40 random databases per query. Coverage was widened on this track (repeated output names, `VALUES`, grouping sets, `LATERAL`, parenthesised joins took it from 657 supported queries), so it measures soundness, not recall.

A failure found later goes into `cases.json` as a regression case before the fix.

Overlap with existing evals: none of the SQLSolver, R-Bot, SQL-IQ or syntax-coverage evals score what a query's output looks like; they score equivalence of two queries. The adapted track reuses their queries only as inputs.
