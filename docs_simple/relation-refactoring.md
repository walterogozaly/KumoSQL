# Refactor readers to a preferred relation

`refactor-project` previews a migration using a saved query-relation declaration. The result is conditional on the declaration's evidence and freshness scope.

```sh
python -m kumosql refactor-project path/to/project --declaration DECLARATION_ID
python -m kumosql refactor-project path/to/project --declaration DECLARATION_ID --write
```

For an unfiltered declaration, the planner supports direct column projections from one table and replaces matching table reads. For filtered declarations, a reader must contain the exact old `SELECT` as a top-level `FROM` or `JOIN` subquery; the preferred query is wrapped with a projection that restores the old output names and order. Formatting, identifier case, and source-table qualification may differ, but the filter and projections must match structurally. Raw-table readers, changed filters, stars, CTEs, and other nested queries stay unchanged.

Both forms update SQLX `ref()` calls and require an `all_snapshots` freshness scope. The planner skips `USING` or `NATURAL` joins, undeclared columns, ambiguous unqualified columns, explicit dependencies on the old relation, and incremental or operation models.

The report records each rewrite operation, declaration ID, evidence, scope, changed readers and skipped readers. `--write` applies changes only after the project reload confirms output names and order. This is conditional on the user's relation declaration; it is not an independent equivalence proof or a warehouse freshness check.

[Full reference](../docs/relation-refactoring.md).
