# Rewrite rules

[All simple guides](README.md) · [Full reference](../docs/rewrite-rules.md)

A rewrite rule makes one kind of change to SQL. For example, `remove_trivial_predicates` removes `WHERE 1 = 1` because it excludes no rows.

```sh
python -m kumosql rewrite-sql query.sql -r remove_trivial_predicates
```

KumoSQL checks each changed result against its input. Read the evidence label before using the output.

## Choose a rule

| Rule | Plain meaning |
| --- | --- |
| `lift_subqueries` | Give a nested FROM/JOIN query a name in `WITH` |
| `inline_single_use_ctes` | Move a `WITH` query used once into the place that reads it |
| `remove_trivial_predicates` | Remove conditions that add nothing, such as `AND TRUE` |
| `remove_redundant_parentheses` | Remove brackets that do not affect meaning |
| `deduplicate_ctes` | Share identical `WITH` queries |
| `remove_unused_ctes` | Remove `WITH` queries nobody reads |
| `remove_redundant_distinct` | Remove `DISTINCT` when supported grouping already prevents duplicates |
| `qualify_columns` | Write `orders.id` instead of bare `id` when a query reads two or more tables and only one has that column (opt in) |
| `format_sql` | Apply saved SQLFluff formatting preferences |

These rules have exceptions. For example, combining duplicate queries containing random calls can change results. A rule may leave such SQL alone.

## Qualify columns

`qualify_columns` is for queries that join tables. `SELECT id, name FROM orders o JOIN customers c ON o.cid = c.cid` becomes `SELECT o.id, c.name ...`, so a reader sees where each column comes from. It does not run in the default pipeline; ask for it with `-r qualify_columns`. Every qualification is also re-checked by a separate checker that confirms only table names were added and that each one names the only table the column can come from ([how](proof-safeguards.md#qualifying-columns)). The rule leaves a column alone when it is a nickname in `GROUP BY` or `ORDER BY`, or is used before its table is read.

It only adds a table name when it is sure. It leaves a column alone when:

- the query reads one table;
- a table's columns are not known;
- the join is `NATURAL` or the column is the one in `USING (...)`, because the merged column has no single table;
- two tables both have the column, or an output name from `SELECT ... AS` is used in `GROUP BY` or `ORDER BY`.

Limits: it needs to know each table's columns, which come from the loaded project or the saved BigQuery catalog, so on the command line only queries built from `WITH` queries and subqueries are qualified. Without those columns the checker may call the result `unproven`. The final `ORDER BY` keeps its bare names, because the checker only accepts a rewrite that leaves the ordering text alone. See the [full reference](../docs/rewrite-rules.md) for the complete list of cases that are skipped.

## Understand the label

| Label | What to do with it |
| --- | --- |
| `unchanged` | The text is identical to the input; a rule may have skipped it, and its step says why. Input that only parsed in the parser's recovery mode (for example a query cut off after `WHERE 1 =`) is labelled `unproven` instead |
| `proven` | Equivalence was established; read any assumptions |
| `planner_checked` | BigQuery could plan the query, but equal results were not proved |
| `unproven` | Review it; the checker could not establish equivalence |
| `failed` | The rewrite failed; its output is not accepted |

Only `unchanged` and `proven` count as trusted. The CLI exits 3 for untrusted output unless you explicitly use `--allow-unproven`; that option does not add evidence. Trusted does not mean the input was valid SQL in general, but a change to a query BigQuery would plainly reject (a column that its subquery does not have, `HAVING` with no grouping or aggregate, or a type name BigQuery does not have such as `FLOAT` or `VARCHAR`, in a cast or elsewhere a type is written) is never `proven`. The check only catches those cases, so it can miss other invalid SQL. Fatal rule failures exit 2 without writing the result.

## Rule order matters

Supply multiple `-r` options to run rules in that order. Put formatting last: other rules can change the layout again.

`--check-idempotence` checks that rerunning the rules makes no further change. It exits 4 if the result changes again. Avoid combining `lift_subqueries` and `inline_single_use_ctes` when you want this property: one lifts a query and the other puts it back.

For Dataform SQLX, the driver protects config, JavaScript, operation blocks, and `${...}` expressions. Formatting with `format_sql` is for SQL, not SQLX. Every formatting change is re-checked by a separate checker that requires the same words, comments and parse tree, so only whitespace and keyword case may differ; selecting sqlfluff rules that change more than layout makes the result `unproven` ([how](proof-safeguards.md#formatting)). A `${...}` expression comes back exactly as written, even when it holds a backslash such as `r'\d'` or `\1`. Unsupported syntax and parse recovery are reported in diagnostics. See [Dataform preservation](evals/dataform-bench.md).

KumoSQL runs on several versions of the SQL parser it is built on (the oldest supported one is 26.0.0, and the tests run on it and on two newer ones). Those versions sometimes give the same piece of SQL different internal names, and code that knew only one name could quietly miss a clause. One example found this way: a `SELECT * EXCEPT (b)` could look like a plain `SELECT *`, so a prover said the two were the same query. The code now reads each construct through shared helpers that know every version's spelling, and where the oldest parser cannot read a piece of SQL at all, the matching test is skipped and says why. The limit: a skipped test means that combination was not checked on the oldest version, not that it passes there. See [Rewrite rules](../docs/rewrite-rules.md) for the helper names.

## Window idioms the prover reads alike

[Full reference](../docs/rewrite-rules.md#window-idioms-the-prover-reads-alike)

People ask for "the latest row per user" in several ways: `ROW_NUMBER() ... = 1`, `ARRAY_AGG(... LIMIT 1)[OFFSET(0)]`, `MAX_BY`, `WHERE (user_id, ts) IN (SELECT user_id, MAX(ts) ... GROUP BY user_id)`, or a join to the grouped `MAX`. The equivalence prover rewrites all of them to the last one (or, when the query only reads the key and the extreme, to a plain `SELECT user_id, MAX(ts) ... GROUP BY user_id`), so two queries written differently can be proved to return the same rows. It does this only when that is exactly true, and leaves the query alone otherwise (the pair then stays "not proven", never "proven by mistake").

Example. With `events(user_id, ts, value)` where `(user_id, ts)` is a declared key:

```sql
SELECT user_id, value FROM events
QUALIFY ROW_NUMBER() OVER (PARTITION BY user_id ORDER BY ts DESC) = 1
```

is read as "each event whose `ts` is the largest for its `user_id`". Without the key, two events of one user can share the largest `ts`. `ROW_NUMBER` then returns just one of them (which one is not defined) while the join returns both, so the prover does not treat them as the same. `RANK() ... = 1` returns every tied row, exactly like the join, so it needs no key. If the query only returns `user_id` and `ts`, ties do not matter even for `ROW_NUMBER`: every tied row looks the same.

The other thing that has to hold is NULL order. An ascending sort puts NULL first but `MIN` skips NULL, so an ascending order on a column that may be NULL is left alone (a descending one is fine). `MAX_BY`/`MIN_BY` also need the compared column and the returned column to be never NULL.

Limits of the evidence: every rewrite is checked by running the query before and after on DuckDB over random small databases with ties, NULLs and empty tables, and for each refusal there is a database where the refused rewrite would give different rows. That is testing, not a proof of the rule. The number of benchmark pairs these rules unlock is in [the VeriEQL page](evals/verieql.md).
