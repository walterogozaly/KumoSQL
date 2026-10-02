"""A filter above a derived outer join that rejects NULLs from the padded side turns the join inner.

``SELECT .. FROM (SELECT a.x, b.y AS c9 FROM a LEFT JOIN b ON ..) d WHERE d.c9 IS NOT NULL``: the
rows the LEFT JOIN pads carry NULL in ``c9``, and the filter drops them, so the derived table may
as well use an inner join. A comparison (``d.c9 > 1``, ``d.c9 = d.c2``) rejects NULL the same way.
For a FULL join, a filter on one side's column leaves the join one-sided. The derived table must
only project, filter and join (no grouping, DISTINCT, LIMIT or window), and the filtered output must
be a bare column of the padded side.

A rule in the same spirit as ``algebraic_equivalence._full_join_to_one_sided``, one level up.
"""

from __future__ import annotations

from sqlglot import exp

_COMPARISONS = (exp.EQ, exp.NEQ, exp.GT, exp.GTE, exp.LT, exp.LTE, exp.Like)


def _conjuncts(node: exp.Expression) -> list[exp.Expression]:
    if isinstance(node, exp.And):
        return _conjuncts(node.left) + _conjuncts(node.right)
    if isinstance(node, exp.Paren):
        return _conjuncts(node.this)
    return [node]


def _rejected_columns(where: exp.Expression, alias: str) -> set[str]:
    """Output columns of ``alias`` that the WHERE clause requires to be non-NULL."""

    found = set()
    for part in _conjuncts(where):
        if isinstance(part, exp.Not) and isinstance(part.this, exp.Is) and isinstance(part.this.expression, exp.Null):
            target = part.this.this
            if isinstance(target, exp.Column) and target.table.lower() == alias:
                found.add(target.name.lower())
        elif isinstance(part, _COMPARISONS):
            for side in (part.this, part.expression):
                if isinstance(side, exp.Column) and side.table.lower() == alias:
                    found.add(side.name.lower())
    return found


def strengthen_derived_outer_join(select: exp.Select) -> exp.Expression | None:
    where = select.args.get("where")
    source = select.args.get("from_") or select.args.get("from")
    if where is None or source is None or not isinstance(source.this, exp.Subquery) or not source.this.alias:
        return None
    inner = source.this.this
    if not isinstance(inner, exp.Select) or any(
        inner.args.get(k) for k in ("group", "having", "distinct", "limit", "offset", "qualify", "order")
    ):
        return None
    if any(n.find_ancestor(exp.Select) is inner for n in inner.find_all(exp.AggFunc, exp.Window)):
        return None
    joins = inner.args.get("joins") or []
    if len(joins) != 1 or joins[0].args.get("kind") or (joins[0].side or "").upper() not in ("LEFT", "RIGHT", "FULL"):
        return None
    inner_from = inner.args.get("from_") or inner.args.get("from")
    left = (inner_from.this.alias_or_name or "").lower()
    right = (joins[0].this.alias_or_name or "").lower()
    if not left or not right or left == right:
        return None
    rejected = _rejected_columns(where.this, source.this.alias.lower())
    sides = set()
    for projection in inner.expressions:
        if projection.alias_or_name.lower() not in rejected:
            continue
        value = projection.this if isinstance(projection, exp.Alias) else projection
        if isinstance(value, exp.Column) and value.table.lower() in (left, right):
            sides.add(value.table.lower())
    side = (joins[0].side or "").upper()
    padded = {"LEFT": {right}, "RIGHT": {left}, "FULL": {left, right}}[side]
    hit = sides & padded
    if not hit:
        return None
    copy = select.copy()
    join = (copy.args.get("from_") or copy.args.get("from")).this.this.args["joins"][0]
    if side != "FULL" or hit == {left, right}:
        join.set("side", None)
    else:
        join.set("side", "RIGHT" if hit == {right} else "LEFT")
    return copy
