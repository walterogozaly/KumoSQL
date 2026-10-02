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
| Column never NULL | Literals, `COUNT`, `ROW_NUMBER`, `IS NULL`; declared NOT NULL columns read through inner joins; `COALESCE` with a non-NULL argument; arithmetic, comparison, `CAST`, `CASE` with an `ELSE`, and null-propagating functions of non-NULL values; `SUM`/`MIN`/`MAX`/`AVG` of non-NULL values under a `GROUP BY` (a group is never empty), but not in a global aggregate (an empty table gives NULL); a column the `WHERE` clause (or an inner join's `ON`) tests with `IS NOT NULL`, a comparison, `BETWEEN`, `LIKE` or `IN`, through `AND` and `OR` (an `OR` needs both sides) |
| Outer joins | The nullable side's columns become nullable again, even ones a subquery had filtered to non-NULL. A later `WHERE` that needs a column of that side to match makes the join an inner join for its rows |
| Unique columns | `GROUP BY` keys (when all are projected, by name, ordinal or alias), `DISTINCT` (the whole row), `UNION`/`INTERSECT`/`EXCEPT` distinct, declared keys of a table read through a join that matches each of its rows at most once (the other side matched on its own key, or on constants and outer references), a pair of keys across a join that can fan out, keys carried by an inner-join or `WHERE` equality |
| Row count | A global aggregate returns exactly one row, `LIMIT 1` and `SELECT` without `FROM` at most/exactly one, a lookup by every column of a key (constants, parameters, or outer columns of a correlated subquery) at most one |
| Scalar subquery | `props.scalar_subquery` is `exactly_one`, `at_most_one` or `unknown` (may return several rows) |

Uniqueness means no two rows are identical with NULL equal to NULL, the way `GROUP BY` and `DISTINCT` compare. Facts that rest on a declared NOT NULL column or key carry it in `assumptions` (`"(id) is unique in orders"`), the same declared facts the prover's results list; facts that follow from the query alone carry none. Unsupported shapes (unknown tables, `UNNEST`, `USING`, grouping sets) return `OutputProperties(unsupported=...)`.

The prover uses it for one thing today: a proof no longer lists "scalar subqueries return at most one row" as an assumption when every shared scalar subquery is shown to return at most one row.

## Evaluation

`python tools/output_properties_bench.py` (development cases), `--held-out` (final evaluation only) and `--adapted` (SQLSolver's queries). The source, labels and checking are described in the tool's docstring; results are in `benchmarks/results/output-properties*.json` and the README scoreboard.

* **Original cases** (`tests/fixtures/output_properties/cases.json`, 74 queries, 138 labelled claims on a shop schema): written from the SQL semantics, including the negative cases (a join destroying uniqueness, `SUM` over an empty table, an outer join undoing a filter's guarantee, a correlated subquery that can return several rows). Every label is checked against executed data: a true label may never be violated on random databases that respect the declarations, and a false label must be violated by some database (51 of 51 are). The analysis is scored `proved` (a true claim found), `unknown` (a true claim missed), `correct_unknown` (a false claim not asserted) and `wrong` (asserted but violated, must stay 0). There is no timeout.
* **Held-out cases** (`held_out.json`, 32 queries, 55 claims on a different schema): written together with the development cases and not run until the analysis was frozen.
* **Adapted queries**: the queries of SQLSolver's Calcite, Spark, TPC-H and TPC-C pairs (pinned in `tests/fixtures/sqlsolver`), analysed with their schemas; there are no labels, so every fact the analysis asserts is checked against executed rows.

A failure found later goes into `cases.json` as a regression case before the fix.

Overlap with existing evals: none of the SQLSolver, R-Bot, SQL-IQ or syntax-coverage evals score what a query's output looks like; they score equivalence of two queries. The adapted track reuses their queries only as inputs.
