# How KumoSQL double-checks its own rewrites

[All simple guides](README.md) · [Full reference](../docs/proof-safeguards.md)

A cleanup rule and the prover that approves it used to share some of the same code. If that code had a bug, the rule could make a wrong change and the prover would agree, because both made the same mistake. The safeguards add a separate checker that shares no code with either, and that refuses a change it cannot confirm.

Suppose a rule removes `AND TRUE` from a filter. The separate checker re-derives, from the query before and after, that the removed part cannot change which rows pass. If it cannot, the rewrite is reported as `unproven`, and a later step that puts the original text back does not hide that.

## Dataform expressions

A Dataform file can hold `${...}` expressions. KumoSQL can treat `${ref("orders")}` as a table name, but anything else (`${when(incremental(), "AND ts > 1")}`, or a value inside quotes such as `'${vars.status}'`) may expand to whole clauses, operators, a comment, or even a quote that ends a string early. A change to a statement holding one, including only moving a line break next to it, is `unproven` until the SQLX is compiled. The provers also refuse text where Dataform expressions were replaced by placeholder names, because the same placeholder can stand for different expressions in different models.

## What this does not cover

Only predicate cleanup is checked independently so far. Other rules and the solver-based provers still rely on their existing checks, and the separate checker does not check BigQuery validity. How the parser's reading of a query is checked is described in [parser checks](parser-checks.md). See the [full reference](../docs/proof-safeguards.md) for the list of audit findings and what is fixed.
