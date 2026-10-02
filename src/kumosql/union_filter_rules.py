"""Push a WHERE over a derived set operation into each of its branches.

``SELECT * FROM (SELECT a, b FROM t UNION SELECT c, d FROM u) AS s WHERE s.b > 1`` is
``SELECT * FROM (SELECT a, b FROM t WHERE b > 1 UNION SELECT c, d FROM u WHERE d > 1) AS s``.
A filter reads only the values of a row, and UNION, INTERSECT and EXCEPT (ALL or DISTINCT) only
keep, merge or drop rows by value, so filtering before or after them leaves the same rows. The
outputs of a set operation are named by its first branch and matched by position, so the filter
is rewritten per branch with that branch's expression for each output.

Only a derived set operation that is the select's only source is read, and only when every branch
is a select that joins, filters, projects and optionally deduplicates; a conjunct moves when it
reads nothing but the set operation's outputs and has no subquery, aggregate, window or random value.
"""

from __future__ import annotations

from sqlglot import exp

_SET_OPERATIONS = (exp.Union, exp.Intersect, exp.Except)
_ROW_CLAUSES = ("order", "limit", "offset", "with_", "with")


def _conjuncts(node: exp.Expression) -> list[exp.Expression]:
    if isinstance(node, exp.And):
        return _conjuncts(node.this) + _conjuncts(node.expression)
    if isinstance(node, exp.Paren) and isinstance(node.this, exp.And):
        return _conjuncts(node.this)
    return [node]


def _and_all(parts: list[exp.Expression]) -> exp.Expression | None:
    result = None
    for part in parts:
        part = exp.Paren(this=part) if isinstance(part, exp.Or) else part
        result = part if result is None else exp.And(this=result, expression=part)
    return result


def _branches(node: exp.Expression) -> list[exp.Select] | None:
    """The selects under a tree of set operations, left to right, or None if any part is not plain."""

    if isinstance(node, exp.Paren):
        return _branches(node.this)
    if isinstance(node, exp.Subquery) and not node.alias:
        return _branches(node.this)
    if isinstance(node, _SET_OPERATIONS):
        if any(node.args.get(k) for k in _ROW_CLAUSES + ("by_name", "side", "kind", "on")):
            return None
        left, right = _branches(node.this), _branches(node.expression)
        return None if left is None or right is None else left + right
    if not isinstance(node, exp.Select):
        return None
    if any(node.args.get(k) for k in ("group", "having", "limit", "offset", "qualify", "windows", "with_", "with", "order", "laterals")):
        return None
    distinct = node.args.get("distinct")
    if distinct is not None and distinct.args.get("on"):
        return None
    if (node.args.get("from_") or node.args.get("from")) is None:
        return None
    for item in node.expressions:
        if isinstance(item, exp.Star) or (isinstance(item, exp.Column) and isinstance(item.this, exp.Star)):
            return None
        if any(isinstance(n, (exp.AggFunc, exp.Window)) for n in item.walk()):
            return None
    return [node]


def _outputs(branch: exp.Select) -> list[exp.Expression]:
    return [item.this if isinstance(item, exp.Alias) else item for item in branch.expressions]


def push_filter_into_set_operation(select: exp.Select) -> exp.Expression | None:
    """``select`` with the WHERE conjuncts over its derived set operation moved into every branch, or None."""

    from_ = select.args.get("from_") or select.args.get("from")
    where = select.args.get("where")
    if from_ is None or where is None or select.args.get("joins") or select.args.get("laterals"):
        return None
    source = from_.this
    if not isinstance(source, exp.Subquery) or not source.alias or not isinstance(source.this, _SET_OPERATIONS):
        return None
    branches = _branches(source.this)
    if not branches:
        return None
    names = [item.alias_or_name.lower() for item in branches[0].expressions]
    if "" in names or len(set(names)) != len(names) or any(len(b.expressions) != len(names) for b in branches):
        return None
    alias = source.alias.lower()
    moved, kept = [], []
    for part in _conjuncts(where.this):
        columns = list(part.find_all(exp.Column))
        movable = (
            columns
            and not any(isinstance(n, (exp.Subquery, exp.Exists, exp.Window, exp.AggFunc, exp.Rand, exp.Anonymous)) for n in part.walk())
            and all(c.name.lower() in names and c.table.lower() in ("", alias) and not isinstance(c.this, exp.Star) for c in columns)
        )
        (moved if movable else kept).append(part)
    if not moved:
        return None
    copy = select.copy()
    new_source = (copy.args.get("from_") or copy.args.get("from")).this
    for branch in _branches(new_source.this) or []:
        values = dict(zip(names, _outputs(branch)))
        pushed = []
        for part in moved:
            holder = exp.Paren(this=part.copy())
            for column in list(holder.find_all(exp.Column)):
                value = values[column.name.lower()]
                column.replace(exp.Paren(this=value.copy()) if not isinstance(value, (exp.Column, exp.Literal)) else value.copy())
            pushed.append(holder.this)
        existing = branch.args.get("where")
        branch.set("where", exp.Where(this=_and_all(([existing.this] if existing is not None else []) + pushed)))
    if kept:
        copy.set("where", exp.Where(this=_and_all(kept)))
        return copy
    copy.set("where", None)
    return _unwrap_identity(copy, alias, names) or copy


def _unwrap_identity(select: exp.Select, alias: str, names: list[str]) -> exp.Expression | None:
    """The set operation itself when ``select`` lists each of its outputs once under its own name.

    Listing them in another order reorders the columns of every branch the same way, and an
    ``ORDER BY`` (without ``LIMIT``) on those outputs orders the set operation instead.
    """

    if any(select.args.get(k) for k in ("where", "group", "having", "distinct", "limit", "offset", "qualify", "windows", "with_", "with")):
        return None
    order = select.args.get("order")
    if order is not None:
        for ordered in order.expressions:
            key = ordered.this if isinstance(ordered, exp.Ordered) else ordered
            if not isinstance(key, exp.Column) or isinstance(key.this, exp.Star) or key.table.lower() not in ("", alias) or key.name.lower() not in names:
                return None
    listed = []
    for item in select.expressions:
        column = item.this if isinstance(item, exp.Alias) else item
        if not isinstance(column, exp.Column) or isinstance(column.this, exp.Star) or column.table.lower() not in ("", alias):
            return None
        if item.alias_or_name.lower() != column.name.lower():
            return None
        listed.append(column.name.lower())
    if sorted(listed) != sorted(names) or len(set(listed)) != len(listed):
        return None
    operation = (select.args.get("from_") or select.args.get("from")).this.this
    if listed != names:
        positions = [names.index(name) for name in listed]
        for branch in _branches(operation) or []:
            items = list(branch.expressions)
            branch.set("expressions", [items[i] for i in positions])
    if select.args.get("order") is not None:
        ordering = select.args["order"].copy()
        for key in list(ordering.find_all(exp.Column)):
            key.replace(exp.column(key.name))
        operation.set("order", ordering)
    return operation
