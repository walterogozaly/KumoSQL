# Refactor readers to a preferred relation

`refactor-project` previews a migration using a saved query-relation declaration. The result is conditional on the declaration's evidence and freshness scope.

```sh
python -m kumosql refactor-project path/to/project --declaration DECLARATION_ID
python -m kumosql refactor-project path/to/project --declaration DECLARATION_ID --write
```

The first version supports direct column projections from one table, with no filter or join on either declaration side. It maps declared columns in supported readers, keeps selected output names and order, and updates SQLX `ref()` calls. The planner skips stars, CTEs and nested queries, `USING` or `NATURAL` joins, undeclared columns, ambiguous unqualified columns, and explicit dependencies on the old relation. It requires an `all_snapshots` freshness scope and refuses incremental or operation models.

The report records the declaration ID, evidence, scope, changed readers and skipped readers. `--write` applies changes only after the project reload confirms output names and order. This is conditional on the user's relation declaration; it is not an independent equivalence proof or a warehouse freshness check.

[Full reference](../docs/relation-refactoring.md).
