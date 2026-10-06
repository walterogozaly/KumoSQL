# Contract-driven relation refactoring

`refactor-project` previews a consumer migration to the preferred side of a saved query-relation declaration. A declaration is an explicit premise from `relation-declarations`; the migration report remains **conditional** on that declaration's evidence and freshness scope.

```sh
python -m kumosql refactor-project path/to/project --declaration DECLARATION_ID
python -m kumosql refactor-project path/to/project --declaration DECLARATION_ID --patch migration.diff
python -m kumosql refactor-project path/to/project --declaration DECLARATION_ID --write
```

For unfiltered declarations, each side may select direct columns from one table, without joins, grouping, `DISTINCT`, or row limits. A supported consumer can replace that table with the preferred table and map each referenced declared column. SQLX `ref()` calls are regenerated with the project's spelling, so inferred dependency edges follow the new relation.

Filtered declarations have a narrower rewrite: a consumer must contain the exact old `SELECT` as a top-level `FROM` or `JOIN` subquery. The planner replaces that whole subquery with the preferred query and adds a projection that restores the old output names and order. Matching ignores formatting, identifier case, and project qualification of the single source table; it requires the filter and projections to match structurally. A raw-table reader, a different filter, or a query with other nested `SELECT`s is left unchanged and listed as skipped. The declaration's mapped output types must match, and the patched project must reload with each changed consumer's output names and order intact.

The planner requires an `all_snapshots` scope; snapshot-, refresh- and incremental-state-scoped declarations need a matching project contract, which this slice does not yet load. It also refuses a migration when its source actions have incremental/pre/post-operation semantics, a consumer's output interface changes, or its SQL needs unsupported scope analysis. It leaves readers unchanged when they use a star, a CTE or nested query outside the exact filtered subquery, a `USING`/`NATURAL` join, an undeclared input column, an unqualified column in a join, or an explicit Dataform action dependency on the old relation (which may encode ordering beyond a read edge). The report lists each changed consumer, the rewrite operation, saved declaration ID, evidence, scope, provenance, and skipped readers. `--write` applies the patch only after reloading the project and checking changed consumers' output names and order.

This check does not independently establish that the declared relations are equivalent or that the declaration applies to the current warehouse snapshot. It does not claim an unconditional prover verdict. Review the declaration's evidence and scope before using the patch.
