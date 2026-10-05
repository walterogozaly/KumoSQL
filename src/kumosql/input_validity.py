"""Refuse a rewrite proof whose input is SQL that parses but BigQuery would reject.

The structural prover compares normalized syntax trees. For a query BigQuery cannot run, "the two trees
match" says nothing about results, so a proof of such a pair would be credited though there is nothing to
preserve. This module finds two kinds of such inputs without any schema, and only when it is certain:

* a ``HAVING`` clause in a select with no ``GROUP BY`` and no aggregate call (BigQuery: "The HAVING clause
  requires GROUP BY or aggregation to be present");
* a column that a derived table or CTE with fully known output names does not produce, referenced as
  ``alias.name`` or, where every source of the select is such a table, as a bare ``name`` (BigQuery:
  "Unrecognized name" / "Name ... not found inside ...").

Anything it cannot see through (real tables, ``SELECT *``, unnamed projections, UNNEST, LATERAL, pivots,
table functions, user-defined aggregates) is treated as unknown and passes: this is a refusal list, not a
validator, so a miss only leaves today's behavior and a hit never rejects valid SQL.
"""

from __future__ import annotations

from sqlglot import exp
from sqlglot.optimizer.scope import Scope, ScopeType, traverse_scope

_OPAQUE_FROM_PARTS = tuple(getattr(exp, name) for name in ("Unnest", "Lateral", "Pivot", "TableFromRows") if hasattr(exp, name))


def invalid_input_reason(statement: exp.Expression) -> str | None:
    """Why ``statement`` could not run on BigQuery, or ``None`` when no problem was found."""

    try:
        scopes = traverse_scope(statement)
    except Exception:  # sqlglot cannot scope the statement: nothing to conclude
        return None
    for scope in scopes:
        select = scope.expression
        if not isinstance(select, exp.Select):
            continue
        reason = _having_reason(select) or _column_reason(scope, select)
        if reason:
            return reason
    return None


def _own_nodes(select: exp.Select, key: str):
    """Nodes under ``select.args[key]`` that are not inside a nested query."""

    value = select.args.get(key)
    for item in value if isinstance(value, list) else [value]:
        if item is not None:
            yield from item.walk(prune=lambda node: isinstance(node, (exp.Subquery, exp.Select)) and node is not item)


def _having_reason(select: exp.Select) -> str | None:
    if select.args.get("having") is None or select.args.get("group") is not None:
        return None
    for key in ("expressions", "having", "order", "qualify"):
        for node in _own_nodes(select, key):
            # User-defined functions may be aggregates; any function sqlglot cannot name is unknown.
            if isinstance(node, (exp.AggFunc, exp.Anonymous, exp.UserDefinedFunction)):
                return None
    return "a HAVING clause with no GROUP BY and no aggregate: BigQuery requires GROUP BY or aggregation"


def _output_names(expression: exp.Expression) -> set[str] | None:
    """The lower-cased output column names of a query, or ``None`` when they are not fully known."""

    while isinstance(expression, (exp.Subquery, exp.Paren)):
        expression = expression.this
    if isinstance(expression, exp.SetOperation):
        return _output_names(expression.left)
    if not isinstance(expression, exp.Select) or expression.args.get("kind") in ("STRUCT", "VALUE"):
        return None
    names: set[str] = set()
    for projection in expression.expressions:
        # Only a bare column or an explicit alias names an output; a literal or an expression is anonymous.
        if projection.find(exp.Star) is not None or not isinstance(projection, (exp.Alias, exp.Column)):
            return None
        names.add(projection.alias_or_name.lower())
    return names


def _source_outputs(source: Scope | exp.Table) -> set[str] | None:
    if not isinstance(source, Scope):
        return None
    expression = source.expression
    parent = expression.parent
    if isinstance(parent, exp.CTE) and parent.args["alias"].args.get("columns"):
        return None
    if isinstance(parent, exp.Subquery) and parent.args.get("alias") and parent.args["alias"].args.get("columns"):
        return None
    return _output_names(expression)


def _from_parts(select: exp.Select) -> list[exp.Expression]:
    parts = []
    from_ = select.args.get("from_") or select.args.get("from")
    if from_ is not None:
        parts.append(from_.this)
    parts.extend(join.this for join in select.args.get("joins") or [])
    return parts


def _column_reason(scope: Scope, select: exp.Select) -> str | None:
    parts = _from_parts(select)
    if not parts or any(isinstance(part, _OPAQUE_FROM_PARTS) or isinstance(part.this if isinstance(part, exp.Table) else None, exp.Func) for part in parts):
        return None
    if any(part.args.get("pivots") for part in parts):
        return None  # PIVOT / UNPIVOT add output columns to their source
    if select.find(exp.Lambda) is not None or select.args.get("laterals"):
        return None
    outputs = {alias.lower(): _source_outputs(source) for alias, source in scope.sources.items()}
    own_aliases = {projection.alias.lower() for projection in select.expressions if isinstance(projection, exp.Alias)}
    all_known = (
        len(parts) == len(scope.sources)
        and all(isinstance(part, (exp.Table, exp.Subquery)) for part in parts)
        and all(known is not None for known in outputs.values())
        and scope.scope_type in (ScopeType.ROOT, ScopeType.DERIVED_TABLE, ScopeType.CTE)
        and not scope.can_be_correlated
    )
    for column in scope.columns:
        if isinstance(column.this, exp.Star) or column.args.get("db") or column.args.get("catalog"):
            continue
        name = column.name.lower()
        if column.table:
            known = outputs.get(column.table.lower())
            if known is not None and name not in known:
                return f"column {column.table}.{column.name} is not an output of {column.table}"
        elif (
            all_known
            and column.find_ancestor(exp.Select) is select
            and name not in own_aliases
            and name not in outputs
            and all(name not in known for known in outputs.values() if known is not None)
        ):
            return f"column {column.name} is not an output of any source in its FROM clause"
    return None
