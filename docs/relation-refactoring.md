# Contract-driven relation refactoring

`refactor-project` previews a consumer migration to the preferred side of a saved query-relation declaration. A declaration is an explicit premise from `relation-declarations`; the migration report remains **conditional** on that declaration's evidence and freshness scope.

```sh
python -m kumosql refactor-project path/to/project --declaration DECLARATION_ID
python -m kumosql refactor-project path/to/project --declaration DECLARATION_ID --patch migration.diff
python -m kumosql refactor-project path/to/project --declaration DECLARATION_ID --write
```

The first supported form has two declaration queries that each select direct columns from one table, without filters, joins, grouping, `DISTINCT`, or row limits. A consumer can replace that table with the preferred table and map each referenced declared column. This preserves the consumer's selected output names and order; the mapped input types must match in the declaration. SQLX `ref()` calls are regenerated with the project's spelling, so inferred dependency edges follow the new relation.

The planner requires an `all_snapshots` scope; snapshot-, refresh- and incremental-state-scoped declarations need a matching project contract, which this slice does not yet load. It also refuses a migration when its source actions have incremental/pre/post-operation semantics, a consumer's output interface changes, or its SQL needs unsupported scope analysis. It leaves readers unchanged when they use a star, a CTE or nested query, a `USING`/`NATURAL` join, an undeclared input column, an unqualified column in a join, or an explicit Dataform action dependency on the old relation (which may encode ordering beyond a read edge). The report lists each changed consumer, the saved declaration ID, evidence, scope, provenance, and skipped readers. `--write` applies the patch only after reloading the project and checking changed consumers' output names and order.

This check does not independently establish that the declared relations are equivalent or that the declaration applies to the current warehouse snapshot. It does not claim an unconditional prover verdict. Review the declaration's evidence and scope before using the patch.
