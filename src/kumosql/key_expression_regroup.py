"""Regroup a grouped derived table by an expression of its keys: ``GROUP BY FLOOR(k TO MINUTE)`` over ``GROUP BY k``.

A view grouped by ``(id, FLOOR(ts TO SECOND))`` answers a query grouped by ``FLOOR(ts TO MINUTE)`` as

    SELECT FLOOR(f TO MINUTE), SUM(s) FROM (SELECT id, FLOOR(ts TO SECOND) AS f, SUM(x) AS s FROM t
                                            GROUP BY id, FLOOR(ts TO SECOND)) AS d GROUP BY FLOOR(f TO MINUTE)

which is ``SELECT FLOOR(ts TO MINUTE), SUM(x) FROM t GROUP BY FLOOR(ts TO MINUTE)``. The existing regrouping rules
read outer keys that are plain columns over inner keys that are plain columns; this reads outer keys that are
expressions of the inner keys' outputs and inner keys that are expressions.

Why it is sound: each outer key is a function of the inner group key, so an outer group is a union of whole inner
groups (NULL keys form one group on both sides). ``SUM`` of the inner ``SUM`` or ``COUNT``, ``MIN`` of the inner
``MIN`` and ``MAX`` of the inner ``MAX`` over those inner groups is the aggregate over all the rows of the outer
group. Every outer group has at least one inner group, so a sum of counts is never NULL. ``AVG``, ``DISTINCT``
aggregates, ``HAVING`` on the inner select and anything non-deterministic are left alone.
"""

from __future__ import annotations

from sqlglot import exp

_BANNED_OUTER = ("distinct", "order", "limit", "offset", "qualify", "windows", "with_", "with", "having", "where", "joins", "pivots", "laterals")
_BANNED_INNER = ("distinct", "order", "limit", "offset", "qualify", "windows", "with_", "with", "having", "pivots", "laterals")
_NONDETERMINISTIC = (exp.Rand, exp.CurrentTimestamp, exp.CurrentDate, exp.CurrentTime, exp.Anonymous, exp.Subquery, exp.Window, exp.AggFunc)


def _plain_group(select: exp.Select) -> list[exp.Expression] | None:
    group = select.args.get("group")
    if group is None or any(group.args.get(k) for k in ("rollup", "cube", "grouping_sets", "totals")) or not group.expressions:
        return None
    return list(group.expressions)


def regroup_by_key_expression(select: exp.Select) -> exp.Expression | None:
    if any(select.args.get(k) for k in _BANNED_OUTER) or any(select.find_all(exp.Window)):
        return None
    outer_keys = _plain_group(select)
    from_ = select.args.get("from_") or select.args.get("from")
    source = from_.this if from_ else None
    if outer_keys is None or not isinstance(source, exp.Subquery) or not isinstance(source.this, exp.Select):
        return None
    inner = source.this
    if any(inner.args.get(k) for k in _BANNED_INNER) or any(inner.find_all(exp.Window)):
        return None
    inner_keys = _plain_group(inner)
    if inner_keys is None:
        return None
    alias = (source.alias or "").lower()
    key_sql = {k.sql(): k for k in inner_keys}
    key_columns: dict[str, exp.Expression] = {}  # inner output name -> the key expression it carries
    aggregates: dict[str, exp.Expression] = {}
    for item in inner.expressions:
        name = (item.alias_or_name or "").lower()
        value = item.this if isinstance(item, exp.Alias) else item
        if not name or name in key_columns or name in aggregates:
            return None
        if isinstance(value, exp.AggFunc):
            if isinstance(value, (exp.Sum, exp.Min, exp.Max, exp.Count)) and not value.args.get("distinct") and not isinstance(value.this, exp.Distinct):
                aggregates[name] = value
            else:
                aggregates[name] = None  # type: ignore[assignment]  # an aggregate that cannot be rolled up, but may be unread
        elif value.sql() in key_sql:
            key_columns[name] = value
        elif any(isinstance(n, exp.AggFunc) for n in value.walk()):
            return None
        else:
            aggregates[name] = None  # type: ignore[assignment]

    def over_keys(node: exp.Expression) -> exp.Expression | None:
        """``node`` with the inner key outputs replaced by their key expressions, or None when it reads anything else."""

        if any(isinstance(n, _NONDETERMINISTIC) for n in node.walk()):
            return None
        copy = node.copy()
        for column in list(copy.find_all(exp.Column)):
            if column.table and column.table.lower() != alias:
                return None
            value = key_columns.get(column.name.lower())
            if value is None:
                return None
            if column is copy:
                copy = value.copy()
            else:
                column.replace(value.copy())
        return copy

    new_keys = []
    for key in outer_keys:
        mapped = over_keys(key)
        if mapped is None or isinstance(mapped, exp.Literal):
            return None
        new_keys.append(mapped)
    mapped_sql = {k.sql() for k in new_keys}

    items = []
    for item in select.expressions:
        expr = item.this if isinstance(item, exp.Alias) else item
        name = item.alias or item.output_name
        mapped = over_keys(expr)
        if mapped is not None and mapped.sql() in mapped_sql:
            value = mapped
        elif isinstance(expr, (exp.Sum, exp.Min, exp.Max)) and not expr.args.get("distinct") and isinstance(expr.this, exp.Column):
            column = expr.this
            if column.table and column.table.lower() != alias:
                return None
            partial = aggregates.get(column.name.lower())
            if partial is None:
                return None
            if isinstance(expr, exp.Sum) and not isinstance(partial, (exp.Sum, exp.Count)):
                return None
            if isinstance(expr, (exp.Min, exp.Max)) and type(partial) is not type(expr):
                return None
            value = partial.copy()
        else:
            return None
        items.append(exp.alias_(value, name) if name else value)
    if not items:
        return None
    result = inner.copy()
    result.set("expressions", items)
    result.set("group", exp.Group(expressions=[k.copy() for k in new_keys]))
    return result
