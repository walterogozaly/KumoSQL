"""A global aggregate over a grouped select whose keys a filter fixes to constants.

``SELECT COALESCE(SUM(c), 0) FROM (SELECT k, COUNT(DISTINCT x) AS c FROM t WHERE k = 'a' GROUP BY k)`` is
``SELECT COUNT(DISTINCT x) FROM t WHERE k = 'a'``.

Why it is sound: every key of the inner select equals a constant (not NULL) in every row it reads, so it has at most
one group: one row when its input has rows, none otherwise. The outer global aggregate then reads that one row or
none. ``SUM`` of the inner ``SUM``, ``MIN`` of the inner ``MIN`` and ``MAX`` of the inner ``MAX`` give the value
over one row and NULL over none, as the inner aggregate computed globally does over no input; ``COALESCE(SUM(c), 0)``
over an inner ``COUNT`` gives the count, and 0 over no row, as ``COUNT`` does. A bare ``SUM`` of a ``COUNT`` is
NULL over no row where ``COUNT`` is 0, so it is not rewritten. ``model_reuse`` leaves this shape when it reads a
model grouped by the filtered key (see ``single_group_reads``).
"""

from __future__ import annotations

from sqlglot import exp

_BANNED = ("distinct", "order", "limit", "offset", "qualify", "windows", "with_", "with", "having", "where", "joins", "pivots", "laterals", "group")
_BANNED_INNER = ("distinct", "order", "limit", "offset", "qualify", "windows", "with_", "with", "having", "pivots", "laterals")


def _conjuncts(node: exp.Expression | None) -> list[exp.Expression]:
    if node is None:
        return []
    if isinstance(node, exp.Paren):
        return _conjuncts(node.this)
    if isinstance(node, exp.And):
        return _conjuncts(node.this) + _conjuncts(node.expression)
    return [node]


def _fixed_columns(where: exp.Expression | None) -> set[str]:
    """Columns the filter equates to a non-NULL constant, by column name and qualifier."""

    fixed: set[str] = set()
    for part in _conjuncts(where.this if isinstance(where, exp.Where) else where):
        if isinstance(part, exp.EQ):
            for column, value in ((part.this, part.expression), (part.expression, part.this)):
                if isinstance(column, exp.Column) and isinstance(value, exp.Literal):
                    fixed.add(column.sql())
    return fixed


def collapse_fixed_key_regroup(select: exp.Select) -> exp.Expression | None:
    if any(select.args.get(k) for k in _BANNED) or any(select.find_all(exp.Window)):
        return None
    from_ = select.args.get("from_") or select.args.get("from")
    source = from_.this if from_ else None
    if not isinstance(source, exp.Subquery) or not isinstance(source.this, exp.Select):
        return None
    inner = source.this
    group = inner.args.get("group")
    if group is None or any(group.args.get(k) for k in ("rollup", "cube", "grouping_sets", "totals")) or not group.expressions:
        return None
    if any(inner.args.get(k) for k in _BANNED_INNER) or any(inner.find_all(exp.Window)) or inner.args.get("where") is None:
        return None
    fixed = _fixed_columns(inner.args["where"])
    if any(not isinstance(k, exp.Column) or k.sql() not in fixed for k in group.expressions):
        return None
    alias = (source.alias or "").lower()
    outputs: dict[str, exp.Expression] = {}
    for item in inner.expressions:
        name = (item.alias_or_name or "").lower()
        if not name or name in outputs:
            return None
        outputs[name] = item.this if isinstance(item, exp.Alias) else item

    def inner_aggregate(column: exp.Expression) -> exp.Expression | None:
        if not isinstance(column, exp.Column) or (column.table and column.table.lower() != alias):
            return None
        value = outputs.get(column.name.lower())
        return value if isinstance(value, (exp.Sum, exp.Min, exp.Max, exp.Count)) else None

    items: list[exp.Expression] = []
    for item in select.expressions:
        expr = item.this if isinstance(item, exp.Alias) else item
        name = item.alias or item.output_name
        if isinstance(expr, exp.Coalesce) and len(expr.expressions) == 1 and isinstance(expr.this, exp.Sum) and not expr.this.args.get("distinct"):
            zero = expr.expressions[0]
            partial = inner_aggregate(expr.this.this)
            if not (isinstance(zero, exp.Literal) and zero.name == "0" and isinstance(partial, exp.Count)):
                return None
            value = partial.copy()
        elif isinstance(expr, (exp.Sum, exp.Min, exp.Max)) and not expr.args.get("distinct") and not isinstance(expr.this, exp.Distinct):
            partial = inner_aggregate(expr.this)
            if partial is None or type(partial) is not type(expr):
                return None
            value = partial.copy()
        else:
            return None
        items.append(exp.alias_(value, name) if name else value)
    if not items:
        return None
    result = inner.copy()
    result.set("expressions", items)
    result.set("group", None)
    return result
