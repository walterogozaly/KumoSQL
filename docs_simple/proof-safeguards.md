# How KumoSQL double-checks its own rewrites

[All simple guides](README.md) · [Full reference](../docs/proof-safeguards.md)

A cleanup rule and the prover that approves it used to share some of the same code. If that code had a bug, the rule could make a wrong change and the prover would agree, because both made the same mistake. The safeguards add a separate checker that shares no code with either, and that refuses a change it cannot confirm.

Suppose a rule removes `AND TRUE` from a filter. The separate checker re-derives, from the query before and after, that the removed part cannot change which rows pass. If it cannot, the rewrite is reported as `unproven`, and a later step that puts the original text back does not hide that.

## CTE rewrites

A `WITH` name can mean a CTE, or a table that happens to have the same name (a CTE is only visible to the CTEs written after it). A rule that guesses wrong would change what the query reads, and the prover could repeat the guess. The separate checker settles it by writing every CTE reference out in full, so `WITH a AS (SELECT 1 x) SELECT * FROM a` becomes `SELECT * FROM (SELECT 1 x) AS a`, and then requires the query before and after a rewrite to be identical. Dropping a CTE nobody reads, inlining one, renaming or merging identical ones leave it unchanged; pointing a reference at the wrong thing does not. It declines recursive queries, a random or clock value that would be read a different number of times, and anything it cannot follow.

On its first run the checker caught a real mistake: `inline_single_use_ctes` replaced a table with a CTE that was defined after the query that read it. That rule is fixed.

## Parentheses and DISTINCT

Two more cleanups are checked the same way. Removing parentheses is accepted only if the query still groups every operator exactly as before, so `(a OR b) AND c` can never quietly become `a OR b AND c`. Removing `DISTINCT` is accepted only if the query already has a plain `GROUP BY` and every grouping column is in the output, so every row is already unique.

## Qualifying columns

`qualify_columns` writes `o.id` in place of a bare `id` when `id` belongs to the table `o`. Picking the owner is easy to get wrong: a name can be a column of two tables, a nickname the query gave to a column (`SELECT x AS y ... GROUP BY y` means the nickname, not some table's `y`), a field inside a struct, or a column of the outer query. If the rule and the prover made the same wrong pick, both would agree on a query that reads a different column.

The separate checker looks at the query before and after and asks two things. First, did anything change except that some columns gained a table name? A changed filter, a renamed alias or a dropped clause hidden in the same step is refused. Second, for each column that gained a name, is that name the only table the column can come from? For example, with `a(x, k)` and `b(y, k)`, `SELECT x, y FROM a JOIN b ON a.k = b.k` may become `SELECT a.x, b.y ...`, but `SELECT b.x ...` is refused, and so is any qualification of `k`, which both tables have. It also refuses a name used as a nickname in `GROUP BY` or `ORDER BY`, a column of a table that is read later than the place it is used (a column in an earlier `ON`), and a query that reads a table it does not know the columns of.

The checker is given a table's column list by the part of KumoSQL that accepts rewrites, from the same project and catalog facts the rule reads, because the query alone does not say what columns a plain table has. It trusts that list, and it trusts that the original query runs on BigQuery. While working out what the checker must refuse, five cases the rule itself had wrong turned up, and the rule was fixed (see the [full reference](../docs/proof-safeguards.md#column-qualification)). The checker can also refuse a valid qualification, for example in a query that reads a table function; the rule leaves those queries alone.

## Columns the provers pick for themselves

The solver-based provers also have to decide, for every bare column, which table it comes from, and a wrong pick would be shared by both sides of a proof just like a rule's. A separate reader now goes through the query text, works out the owner of each bare column on its own, and compares it with what the prover picked: the SMT prover's choice for each column, the places where the algebraic prover writes `a.x` for a bare `x`, and sqlglot's own qualifier in one of the fallback checks. Example: with `t(a, b)` and `u(a, c)`, in `SELECT t.b FROM t WHERE EXISTS (SELECT 1 FROM u WHERE a = 1)` the `a` is `u`'s, because a subquery's own table wins over the query around it. A prover that read it as `t.a` would "prove" this query equal to one that really tests `t.a`; the reader sees that `a` belongs to `u`, and the pair becomes `not_proven`.

The reader says so only when it can tell. If it does not know a table's columns, or the name could mean several things (a table's own name, a nickname the query gave a column, a column of the outer query), it stays out of the way and the proof stands, because refusing all of those would throw away correct proofs. The full reference gives how many columns it could decide on the benchmark sets and exactly which places it covers. It does not cover the many rewrites inside the algebraic prover that rebuild qualified columns while reshaping a query, nor how a qualified name like `a.x` finds `a`.

## One list of checked rules

KumoSQL keeps a single reviewed list of which cleanup rules have a separate checker and which do not yet, with the reason for each one that does not. A test fails if someone adds a rule and forgets to put it on the list, so a new rule has to choose between getting a checker and saying what it relies on instead.

## Dataform expressions

A Dataform file can hold `${...}` expressions. KumoSQL can treat `${ref("orders")}` as a table name, but anything else (`${when(incremental(), "AND ts > 1")}`, or a value inside quotes such as `'${vars.status}'`) may expand to whole clauses, operators, a comment, or even a quote that ends a string early. A change to a statement holding one, including only moving a line break next to it, is `unproven` until the SQLX is compiled. The provers also refuse text where Dataform expressions were replaced by placeholder names, because the same placeholder can stand for different expressions in different models.

## What this does not cover

Predicate cleanup, CTE rewrites, parentheses, removing `DISTINCT` and qualifying columns are checked independently so far, and so is the table the provers pick for a bare column in the places listed above. The step that turns subqueries into CTEs, and the rule that formats SQL, are not. Other rules and the solver-based provers still rely on their existing checks, and the separate checker does not check BigQuery validity. See the [full reference](../docs/proof-safeguards.md) for the list of audit findings and what is fixed. How the parser's reading of a query is checked is described in [parser checks](parser-checks.md).
