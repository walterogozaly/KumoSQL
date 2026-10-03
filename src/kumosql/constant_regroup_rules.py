"""Collapse a constant regrouping of a constant grouping: ``GROUP BY TRUE`` over ``GROUP BY TRUE``.

``SELECT SUM(c) AS n, 1 AS k FROM (SELECT COUNT(x) AS c FROM t GROUP BY TRUE) AS d GROUP BY TRUE``
is ``SELECT COUNT(x) AS n, 1 AS k FROM t GROUP BY TRUE``. Pushing an aggregate into the branches of a
``UNION ALL`` whose branches each carry their own constant group key leaves this shape in every branch.
"""

from __future__ import annotations

from sqlglot import exp

_BANNED = ("distinct", "order", "limit", "offset", "qualify", "windows", "with_", "with", "having", "where", "joins", "pivots", "laterals")


def _constant_grouping(select: exp.Select) -> bool:
    group = select.args.get("group")
    if group is None or any(group.args.get(k) for k in ("rollup", "cube", "grouping_sets", "totals")):
        return False
    # ordinals are resolved before the rules run, so a number left in GROUP BY is a constant (see _constant_keys)
    return bool(group.expressions) and all(
        isinstance(key, exp.Boolean) or isinstance(key, exp.Literal) and not key.is_string for key in group.expressions
    )


def collapse_constant_regroup(select: exp.Select) -> exp.Expression | None:
    """``SELECT agg(d.c).., consts FROM (SELECT .. GROUP BY TRUE) AS d GROUP BY TRUE`` reads the inner row.

    Why it is sound, under bag semantics: the inner select groups by constants only, so it returns one row
    when its input has rows and none when it is empty. The outer select groups that by constants only, so
    it returns one group for the one inner row and no group (no row) for none. Over a single row ``SUM``,
    ``MIN`` and ``MAX`` of a value are that value (NULL included), and a constant reads the same. So the
    outer select is the inner select with its outputs replaced, keeping the inner ``GROUP BY TRUE`` and with
    it the empty-input case (no row, not a zero row). The outer select must have no WHERE, HAVING, join or
    DISTINCT, and only reads ``SUM`` of an inner ``SUM``/``COUNT`` (both numeric) or ``MIN``/``MAX`` of an
    inner aggregate.
    """

    if not _constant_grouping(select) or any(select.args.get(k) for k in _BANNED) or any(select.find_all(exp.Window)):
        return None
    from_ = select.args.get("from_") or select.args.get("from")
    source = from_.this if from_ else None
    if not isinstance(source, exp.Subquery) or not isinstance(source.this, exp.Select):
        return None
    inner = source.this
    if not _constant_grouping(inner) or any(inner.args.get(k) for k in ("distinct", "order", "limit", "offset", "qualify", "windows", "with_", "with")):
        return None
    if any(inner.find_all(exp.Window)):
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
        return value if isinstance(value, exp.AggFunc) else None

    items: list[exp.Expression] = []
    for item in select.expressions:
        expr = item.this if isinstance(item, exp.Alias) else item
        name = item.alias or item.output_name
        if isinstance(expr, (exp.Null, exp.Boolean)) or isinstance(expr, exp.Literal):
            value = expr.copy()
        elif isinstance(expr, (exp.Sum, exp.Min, exp.Max)) and not expr.args.get("distinct"):
            argument = expr.this.expressions[0] if isinstance(expr.this, exp.Distinct) and len(expr.this.expressions) == 1 else expr.this
            value = inner_aggregate(argument)
            if value is None or (isinstance(expr, exp.Sum) and not isinstance(value, (exp.Sum, exp.Count))):
                return None
            value = value.copy()
        else:
            return None
        items.append(exp.alias_(value, name) if name else value)
    result = inner.copy()
    result.set("expressions", items)
    return result
