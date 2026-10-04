# How KumoSQL double-checks its own rewrites

[All simple guides](README.md) · [Full reference](../docs/proof-safeguards.md)

A cleanup rule and the prover that approves it used to share some of the same code. If that code had a bug, the rule could make a wrong change and the prover would agree, because both made the same mistake. The safeguards add a separate checker that shares no code with either, and that refuses a change it cannot confirm.

Suppose a rule removes `AND TRUE` from a filter. The separate checker re-derives, from the query before and after, that the removed part cannot change which rows pass. If it cannot, the rewrite is reported as `unproven`, and a later step that puts the original text back does not hide that.

## CTE rewrites

A `WITH` name can mean a CTE, or a table that happens to have the same name (a CTE is only visible to the CTEs written after it). A rule that guesses wrong would change what the query reads, and the prover could repeat the guess. The separate checker settles it by writing every CTE reference out in full, so `WITH a AS (SELECT 1 x) SELECT * FROM a` becomes `SELECT * FROM (SELECT 1 x) AS a`, and then requires the query before and after a rewrite to be identical. Dropping a CTE nobody reads, inlining one, renaming or merging identical ones leave it unchanged; pointing a reference at the wrong thing does not. It declines recursive queries, a random or clock value that would be read a different number of times, and anything it cannot follow.

On its first run the checker caught a real mistake: `inline_single_use_ctes` replaced a table with a CTE that was defined after the query that read it. That rule is fixed.

## Parentheses and DISTINCT

Two more cleanups are checked the same way. Removing parentheses is accepted only if the query still groups every operator exactly as before, so `(a OR b) AND c` can never quietly become `a OR b AND c`. Removing `DISTINCT` is accepted only if the query already has a plain `GROUP BY` and every grouping column is in the output, so every row is already unique.

## Turning subqueries into CTEs

`lift_subqueries` rewrites `SELECT * FROM (SELECT x FROM t WHERE x > 1) AS s` as `WITH __lifted_subquery_001 AS (SELECT x FROM t WHERE x > 1) SELECT * FROM __lifted_subquery_001 AS s`. The prover used the same lifter on both sides of a proof, so a lifter that dropped the `WHERE`, gave the new CTE the name of a real table, or moved a subquery somewhere it reads something else would still make both sides look alike.

The separate checker undoes the lift: it writes each new CTE back as the subquery it replaced and requires the result to be exactly the original query. The new name must appear nowhere in the original, each new CTE must be read exactly once, the original CTEs must be untouched, and the names must still mean the same thing where the CTE now sits (a subquery that read a nested `WITH`'s name, or a column of the query around it, cannot be moved to the top). A new CTE that calls `RAND()` inside a scalar subquery is refused too, because the subquery would have been evaluated for each outer row.

Running it over about 2,900 test queries found real problems, now fixed: the prover moved a correlated subquery out of the query it depends on, the lifter dropped the column names in `(...) AS t (a, b)`, and it turned `FROM (t)` into the invalid `WITH l AS (t)`. One valid query is still refused: a lift in a statement that holds `WITH RECURSIVE`. The checker also cannot tell when a bare, unqualified column of a subquery comes from the query around it, because that needs a table schema.

## One list of checked rules

KumoSQL keeps a single reviewed list of which cleanup rules have a separate checker and which do not yet, with the reason for each one that does not. A test fails if someone adds a rule and forgets to put it on the list, so a new rule has to choose between getting a checker and saying what it relies on instead.

## Dataform expressions

A Dataform file can hold `${...}` expressions. KumoSQL can treat `${ref("orders")}` as a table name, but anything else (`${when(incremental(), "AND ts > 1")}`, or a value inside quotes such as `'${vars.status}'`) may expand to whole clauses, operators, a comment, or even a quote that ends a string early. A change to a statement holding one, including only moving a line break next to it, is `unproven` until the SQLX is compiled. The provers also refuse text where Dataform expressions were replaced by placeholder names, because the same placeholder can stand for different expressions in different models.

## What this does not cover

Predicate cleanup, CTE rewrites, parentheses, removing `DISTINCT` and turning subqueries into CTEs are checked independently so far. The rules that format SQL or qualify columns are not. Other rules and the solver-based provers still rely on their existing checks, and the separate checker does not check BigQuery validity. See the [full reference](../docs/proof-safeguards.md) for the list of audit findings and what is fixed. How the parser's reading of a query is checked is described in [parser checks](parser-checks.md).
