# Reading SQL scripts and MERGE

[All simple guides](README.md) · [Full reference](../docs/scripts.md)

A script contains several statements. They can create temporary tables, set variables, or change a target table. Reading only the last SELECT can miss where its data came from.

## Follow the data through the script

Consider:

```sql
CREATE TEMP TABLE recent AS
SELECT id, amount FROM orders WHERE amount > 0;

CREATE TABLE report AS
SELECT id, amount FROM recent;
```

KumoSQL remembers the query that created `recent`, so it can trace `report.amount` back to `orders.amount`.

Its splitter understands strings, comments, and nested blocks. A semicolon inside a string or an IF block is not treated as a top-level statement boundary. Loops are followed once for dependency analysis; that does not simulate all iterations.

## How MERGE is read

For a supported MERGE, KumoSQL follows values assigned by each WHEN clause into the target's columns. It considers the source, join condition, and clause filters.

This describes lineage. It does not establish that the script produces correct business results or simulate every possible runtime effect.

## What makes the result incomplete?

Table changes retain the tables they read and write. For example, creating `new` from `old` with LIKE or CLONE
connects those tables; renaming `old` to `new` keeps both names. Dropping or truncating a table records its output
without inventing an input. A script can write several tables, even though only its final supported output has
column lineage. Stored nested fields still trace to their containing column.

- A table named like a `WITH` table is a real table whenever the `WITH` does not cover that spot: a temporary table `tmp` read next to a nested `WITH tmp AS (...)` is still traced through its script statement. When a statement does not parse and its tables come from tokens, a name is skipped only where a `WITH` of that name is in scope.
- Dynamic SQL may build a table name at runtime. KumoSQL does not guess it.
- Updating or merging a temporary table can make its column lineage unknown while its table dependencies remain visible.
- Unresolved Dataform expressions and unsupported statements are reported.
- A parse failure may still retain table dependencies recovered from tokens, with a diagnostic explaining that columns are unknown. A `FROM` that is part of a function, as in `EXTRACT(DATE FROM ts)` or `TRIM(BOTH 'x' FROM s)`, or of `IS DISTINCT FROM`, is not mistaken for a table.

Diagnostics distinguish informational script summaries from gaps that block completeness. Check those gaps before concluding that a table or column has no readers.

The full reference covers variables, table functions, exports, statement roles, and MERGE cases. The script evaluation creates scripts with known dependencies, including tricky strings and temporary-table chains, to check the splitter and lineage.
