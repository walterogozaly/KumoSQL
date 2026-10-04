# How KumoSQL double-checks its own rewrites

[All simple guides](README.md) · [Full reference](../docs/proof-safeguards.md)

A cleanup rule and the prover that approves it used to share some of the same code. If that code had a bug, the rule could make a wrong change and the prover would agree, because both made the same mistake. The safeguards add a separate checker that shares no code with either, and that refuses a change it cannot confirm.

Suppose a rule removes `AND TRUE` from a filter. The separate checker re-derives, from the query before and after, that the removed part cannot change which rows pass. If it cannot, the rewrite is reported as `unproven`, and a later step that puts the original text back does not hide that.

## CTE rewrites

A `WITH` name can mean a CTE, or a table that happens to have the same name (a CTE is only visible to the CTEs written after it). A rule that guesses wrong would change what the query reads, and the prover could repeat the guess. The separate checker settles it by writing every CTE reference out in full, so `WITH a AS (SELECT 1 x) SELECT * FROM a` becomes `SELECT * FROM (SELECT 1 x) AS a`, and then requires the query before and after a rewrite to be identical. Dropping a CTE nobody reads, inlining one, renaming or merging identical ones leave it unchanged; pointing a reference at the wrong thing does not. It declines recursive queries, a random or clock value that would be read a different number of times, and anything it cannot follow.

On its first run the checker caught a real mistake: `inline_single_use_ctes` replaced a table with a CTE that was defined after the query that read it. That rule is fixed.

## Parentheses and DISTINCT

Two more cleanups are checked the same way. Removing parentheses is accepted only if the query still groups every operator exactly as before, so `(a OR b) AND c` can never quietly become `a OR b AND c`. Removing `DISTINCT` is accepted only if the query already has a plain `GROUP BY` and every grouping column is in the output, so every row is already unique.

## One list of checked rules

KumoSQL keeps a single reviewed list of which cleanup rules have a separate checker and which do not yet, with the reason for each one that does not. A test fails if someone adds a rule and forgets to put it on the list, so a new rule has to choose between getting a checker and saying what it relies on instead.

## Dataform expressions

A Dataform file can hold `${...}` expressions. KumoSQL can treat `${ref("orders")}` as a table name, but anything else (`${when(incremental(), "AND ts > 1")}`, or a value inside quotes such as `'${vars.status}'`) may expand to whole clauses, operators, a comment, or even a quote that ends a string early. A change to a statement holding one, including only moving a line break next to it, is `unproven` until the SQLX is compiled. The provers also refuse text where Dataform expressions were replaced by placeholder names, because the same placeholder can stand for different expressions in different models.

## What this does not cover

Predicate cleanup, CTE rewrites, parentheses and removing `DISTINCT` are checked independently so far. The step that turns subqueries into CTEs, and the rules that format SQL or qualify columns, are not. Other rules and the solver-based provers still rely on their existing checks, and the separate checker does not check BigQuery validity or the parser. See the [full reference](../docs/proof-safeguards.md) for the list of audit findings and what is fixed.
