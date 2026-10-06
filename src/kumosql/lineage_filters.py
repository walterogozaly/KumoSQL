"""Row filters attached to column lineage records."""

from __future__ import annotations

from collections.abc import Callable

from sqlglot import exp
from sqlglot.optimizer.scope import Scope, build_scope

from .pipeline_types import ColumnRef, LineageFilter


def collect_lineage_filters(
    query: exp.Expression,
    resolve_table: Callable[[exp.Table], str],
    field_path: Callable[[exp.Column], tuple[str, ...]],
) -> tuple[LineageFilter, ...]:
    """Collect filters from the query scopes used to produce ``query``'s rows and values.

    Unused CTE definitions are skipped. References through a CTE or derived table are
    followed through its projections to the physical columns available to the analysis.
    """

    try:
        root = build_scope(query)
    except Exception:
        return ()
    if root is None:
        return ()

    found: set[LineageFilter] = set()
    visited: set[int] = set()

    def resolve_source_column(
        source: exp.Table | Scope,
        name: str,
        path: tuple[str, ...] = (),
        seen: frozenset[tuple[int, str]] = frozenset(),
    ) -> set[ColumnRef]:
        if isinstance(source, exp.Table):
            if isinstance(source.this, exp.Func):
                return set()
            return {ColumnRef(resolve_table(source), name, path)}

        marker = (id(source), name.casefold())
        if marker in seen:
            return set()
        seen = seen | {marker}

        branches = getattr(source, "set_operation_scopes", ()) or ()
        if branches:
            result: set[ColumnRef] = set()
            for branch in branches:
                result.update(resolve_output_column(branch, name, path, seen))
            return result
        return resolve_output_column(source, name, path, seen)

    def output_items(scope: Scope) -> list[tuple[str, exp.Expression]]:
        expression = scope.expression
        items = list(getattr(expression, "expressions", ()) or ())
        names = list(getattr(scope, "outer_columns", ()) or ())
        if not items:
            return []
        if len(names) != len(items):
            names = [item.alias_or_name for item in items]
        return [(str(name), item) for name, item in zip(names, items)]

    def resolve_output_column(
        scope: Scope,
        name: str,
        path: tuple[str, ...],
        seen: frozenset[tuple[int, str]],
    ) -> set[ColumnRef]:
        branches = getattr(scope, "set_operation_scopes", ()) or ()
        if branches:
            result: set[ColumnRef] = set()
            for branch in branches:
                result.update(resolve_output_column(branch, name, path, seen))
            return result

        matches = []
        for output, item in output_items(scope):
            projection = item.this if isinstance(item, exp.Alias) else item
            if output.casefold() == name.casefold() or projection.is_star:
                matches.append(item)
        result: set[ColumnRef] = set()
        for item in matches:
            projection = item.this if isinstance(item, exp.Alias) else item
            if isinstance(projection, exp.Column):
                result.update(resolve_column(scope, projection, seen, path))
                continue
            if isinstance(projection, exp.Star) or projection.is_star:
                for source in scope.sources.values():
                    result.update(resolve_source_column(source, name, path, seen))
                continue
            for column in projection.find_all(exp.Column):
                result.update(resolve_column(scope, column, seen))
        return result

    def source_for_column(scope: Scope, column: exp.Column) -> tuple[str, exp.Table | Scope] | None:
        current: Scope | None = scope
        while current is not None:
            if column.table:
                for alias, source in current.sources.items():
                    if alias.casefold() == column.table.casefold():
                        return alias, source
            else:
                candidates = list(current.sources.items())
                if len(candidates) == 1:
                    return candidates[0]
            current = current.parent
        return None

    def resolve_column(
        scope: Scope,
        column: exp.Column,
        seen: frozenset[tuple[int, str]] = frozenset(),
        extra_path: tuple[str, ...] = (),
        expand_alias: bool = False,
    ) -> set[ColumnRef]:
        if expand_alias and isinstance(scope.expression, exp.Select):
            for item in scope.expression.expressions:
                if not isinstance(item, exp.Alias) or item.alias.casefold() != column.name.casefold():
                    continue
                projection = item.this
                if isinstance(projection, exp.Column):
                    return resolve_column(scope, projection, seen, extra_path)
                result: set[ColumnRef] = set()
                for dependency in projection.find_all(exp.Column):
                    result.update(resolve_column(scope, dependency, seen))
                return result
        bound = source_for_column(scope, column)
        if bound is None:
            return set()
        _alias, source = bound
        return resolve_source_column(source, column.name, (*field_path(column), *extra_path), seen)

    def label_for_source(source: Scope, alias: str) -> tuple[str, bool]:
        parent = source.expression.parent
        if isinstance(parent, exp.CTE):
            return f"CTE {parent.alias_or_name or alias}", False
        if isinstance(parent, exp.Subquery):
            name = parent.alias_or_name or alias
            if name:
                return f"derived table {name}", False
            return "subquery", True
        kind = str(getattr(source, "scope_type", "")).casefold()
        if "cte" in kind:
            return f"CTE {getattr(source, 'name', '') or alias}", False
        if "derived" in kind or "subquery" in kind:
            return (f"derived table {alias}" if alias else "subquery"), not bool(alias)
        return (f"derived table {alias}" if alias else "subquery"), not bool(alias)

    def relation_aliases(node: exp.Expression | None) -> list[str]:
        if node is None:
            return []
        node = node.this if isinstance(node, exp.From) else node
        alias = node.alias_or_name
        if alias:
            return [str(alias)]
        if isinstance(node, exp.Join):
            return relation_aliases(node.this)
        return []

    def effect_for(base: str, feed_value: bool) -> str:
        return "feeds_value" if feed_value else base

    def add_filter(
        scope: Scope,
        label: str,
        clause: str,
        expression: exp.Expression,
        base_effect: str,
        feed_value: bool,
    ) -> None:
        sources: set[ColumnRef] = set()
        for column in expression.find_all(exp.Column):
            sources.update(resolve_column(scope, column, expand_alias=clause in {"HAVING", "QUALIFY"}))
        found.add(
            LineageFilter(
                scope=label,
                clause=clause,
                sources=tuple(sorted(sources)),
                effect=effect_for(base_effect, feed_value),
            )
        )

    def visit(scope: Scope, label: str, feed_value: bool = False) -> None:
        if id(scope) in visited:
            return
        visited.add(id(scope))

        expression = scope.expression
        branches = getattr(scope, "set_operation_scopes", ()) or ()
        if branches:
            for index, branch in enumerate(branches, 1):
                visit(branch, f"{label} / set-operation branch {index}", feed_value)
        elif isinstance(expression, exp.Select):
            for key, clause in (("where", "WHERE"), ("having", "HAVING"), ("qualify", "QUALIFY")):
                node = expression.args.get(key)
                value = node.this if isinstance(node, exp.Expression) and node.args.get("this") is not None else node
                if isinstance(value, exp.Expression):
                    add_filter(scope, label, clause, value, "limits_rows", feed_value)

            prior_aliases = relation_aliases(expression.args.get("from_"))
            for join in expression.args.get("joins") or ():
                join_effect = "excludes_rows" if any(
                    "anti" in str(join.args.get(key, "")).casefold()
                    for key in ("kind", "side", "method")
                ) else "matches_only"
                on = join.args.get("on")
                if isinstance(on, exp.Expression):
                    add_filter(scope, label, "JOIN ON", on, join_effect, feed_value)
                using = join.args.get("using") or ()
                if using:
                    aliases = [*prior_aliases, *relation_aliases(join.this)]
                    columns: set[ColumnRef] = set()
                    for item in using:
                        name = item.name if isinstance(item, exp.Identifier) else str(item)
                        for alias in aliases:
                            source = next(
                                (value for key, value in scope.sources.items() if key.casefold() == alias.casefold()),
                                None,
                            )
                            if source is not None:
                                columns.update(resolve_source_column(source, name))
                    found.add(
                        LineageFilter(
                            scope=label,
                            clause="JOIN USING",
                            sources=tuple(sorted(columns)),
                            effect=effect_for(join_effect, feed_value),
                        )
                    )
                prior_aliases.extend(relation_aliases(join.this))

        # Scope references omit unused CTE definitions. Use references rather than
        # selected_sources so a CTE on the RHS of a semi/anti join is included too.
        for alias, _node in getattr(scope, "references", ()):
            source = scope.sources.get(alias)
            if isinstance(source, Scope):
                child_label, child_feeds_value = label_for_source(source, str(alias))
                visit(source, f"{label} / {child_label}", child_feeds_value)
        # Scalar and predicate subqueries are values of their containing expression, not CTE inputs.
        for child in getattr(scope, "subquery_scopes", ()) or ():
            if not isinstance(child, Scope):
                continue
            child_label, _child_feeds_value = label_for_source(child, "")
            visit(child, f"{label} / {child_label}", True)

    try:
        visit(root, "query")
    except Exception:
        # A query can still have useful value lineage when a scope cannot be resolved;
        # row-filter detail stays empty rather than interrupting the pipeline analysis.
        return ()
    return tuple(sorted(found, key=lambda item: (item.scope, item.clause, item.effect, item.sources)))
