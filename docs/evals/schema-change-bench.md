# Schema-change compatibility suite

[Plain-language version](../../docs_simple/evals/schema-change-bench.md)

Question: if a table gains, loses, renames or retypes a column, which downstream models break and which change their
output columns or types? `Pipeline.assess_schema_change(kind, table, column, new_name=..., new_type=...)` answers it
(`src/kumosql/schema_change.py`). It re-resolves every model that reads the table, in dependency order, against the
changed schema and compares with how the model resolves today.

- `breaks`: the model resolved before and does not now (named column gone, bare column ambiguous, duplicate output
  names, `* EXCEPT`/`REPLACE` of a missing column, `UNION` arm width differs).
- `output_changes`: still works, but its output columns or types differ (the `SELECT *` cases), with exact added,
  removed and retyped columns.
- `unknown`: cannot be judged (model did not parse and names the table, unknown input columns, unresolved template),
  plus everything that reads such a model. Unknown is never reported as safe.

A model that breaks is assumed fixed with its current output, so models after it are judged on their own.

## Output types

A model's output types come from sqlglot's `qualify` and `annotate_types`, corrected by the GoogleSQL type checker
(`kumosql.googlesql_types`) run on the query as written. The checker's type replaces sqlglot's only where sqlglot said
`UNKNOWN` or named a different type, and only when the checker's type is known and complete, its output columns match
sqlglot's in number and name, and it did not fail; where the two agree sqlglot's own text is kept, and where the checker
says unknown the old answer stands. This matters most for set operations (sqlglot types a `UNION` by its first arm, so a
retype of the second arm's column went unseen), `d1 - d2` over dates (an `INTERVAL`, not a `DATE`) and functions such
as `TRUNC` that sqlglot leaves unknown. `tests/test_googlesql_type_hooks.py` has one case of each.

## The suite

`python tools/schema_change_bench.py [--write-results]` generates pipelines of 8, 30 and 120 models (3 seeds each).
Every model is a spec that prints its SQL and resolves itself against its inputs' columns, so the answer key comes from
a simulator that never parses SQL.

| Split | Families |
| --- | --- |
| dev | explicit projection, `SELECT *`, expressions and casts, aggregates, CTE star, join with bare column, unparsable model |
| held out | `SELECT *, expr`, `* EXCEPT ... REPLACE`, `x.*` join, `UNION ALL` of stars |

Scores are kept apart: correctness (models that break or change but are reported safe, false breaks: both must be 0),
analysis (precision and recall of breaks and output changes, exact columns), coverage (scenarios declined as unknown),
performance (ms per scenario).

Overlap: `assess_change` (docs/lineage-bench.md) covers drop/rename/expression change by column reads; this suite adds
add/retype, `SELECT *` propagation and output-schema prediction. Original and adapted cases are not involved: the
suite is generated, nothing is imported.

Retypes that feed a `UNION ALL` of two `SELECT *` arms are scored too: the key gives each output the numeric supertype of
its arms (`INT64` < `NUMERIC` < `FLOAT64`). Without the type checker those 360 held-out scenarios give 351 exact, 5 models
reported unaffected whose output type changed, and output-change precision 0.976; with it, 360 exact, 0 wrong
(`union_star` is a held-out family, but these scenarios were added after the checker was hooked in, so they are not
held out from it). Totals are 681/720 (360 dev, 360 held out; 662/701 before).

Held-out first run: 2 misses (a `* EXCEPT (col)` over a dropped column, which BigQuery rejects and sqlglot ignores),
fixed afterwards; those families no longer count as held out. Limits: retypes are checked only where the type reaches
an output column, and no set operation other than that `UNION ALL` of stars is generated.
