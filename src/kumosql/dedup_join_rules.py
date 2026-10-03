"""Drop an outer join whose far side is never read, when duplicates cannot matter.

``a LEFT JOIN b ON c`` keeps every row of ``a`` at least once; when nothing
reads ``b``'s columns (outside ``c``), the join only repeats rows of ``a``. A
query that cannot see repeats (``DISTINCT``, or ``GROUP BY`` whose aggregates
are all ``MIN``/``MAX``/``DISTINCT`` ones) returns the same rows without it.
``RIGHT JOIN`` is the mirror image. The join may sit in the duplicate-blind
select itself or in a derived table it reads that only projects and filters.
"""

from __future__ import annotations

from sqlglot import exp

from .ast_utils import FROM_KEY

_BLIND = (exp.Min, exp.Max)


def _duplicate_blind(select: exp.Select) -> bool:
    if select.args.get("distinct") is not None and not select.args["distinct"].args.get("on"):
        return not any(w.find_ancestor(exp.Select) is select for w in select.find_all(exp.Window))
    if not select.args.get("group"):
        return False
    for node in select.find_all(exp.AggFunc):
        if node.find_ancestor(exp.Select) is not select:
            continue
        if isinstance(node, _BLIND):
            continue
        if isinstance(node, (exp.Count, exp.Sum, exp.Avg)) and isinstance(node.this, exp.Distinct):
            continue
        return False
    return not any(w.find_ancestor(exp.Select) is select for w in select.find_all(exp.Window))


def _name(source: exp.Expression) -> str | None:
    alias = source.args.get("alias")
    if alias is not None and alias.name:
        return alias.name
    return source.name if isinstance(source, exp.Table) else None


def _reads(select: exp.Select, name: str, skip: exp.Expression) -> bool:
    for column in select.find_all(exp.Column):
        if column.table == name and not _inside(column, skip):
            return True
    return False


def _inside(node: exp.Expression, root: exp.Expression) -> bool:
    while node is not None:
        if node is root:
            return True
        node = node.parent
    return False


def _drop_join(select: exp.Select) -> exp.Select | None:
    """``select`` with one unread LEFT/RIGHT-joined side removed, or None."""

    joins = select.args.get("joins") or []
    if len(joins) != 1 or select.args.get("laterals"):
        return None
    join = joins[0]
    side = (join.side or "").upper()
    if side not in ("LEFT", "RIGHT") or join.args.get("kind") or join.args.get("using"):
        return None
    source = (select.args.get("from_") or select.args.get("from")).this
    far, near = (join.this, source) if side == "LEFT" else (source, join.this)
    name = _name(far)
    if not name or not _name(near) or isinstance(far, exp.Lateral) or isinstance(near, exp.Lateral):
        return None
    if _reads(select, name, join.args.get("on")):
        return None
    if any(isinstance(c.this, exp.Star) for c in select.find_all(exp.Column)) or any(
        isinstance(s, exp.Star) for s in select.expressions
    ):
        return None
    copy = select.copy()
    copy.set("joins", None)
    copy.set(FROM_KEY, exp.From(this=near.copy()))
    return copy


def drop_unread_outer_join(select: exp.Select) -> exp.Expression | None:
    if not _duplicate_blind(select):
        return None
    own = _drop_join(select)
    if own is not None:
        return own
    source = select.args.get("from_") or select.args.get("from")
    if source is None or select.args.get("joins") or not isinstance(source.this, exp.Subquery):
        return None
    inner = source.this.this
    if not isinstance(inner, exp.Select) or any(
        inner.args.get(k) for k in ("group", "having", "distinct", "limit", "offset", "qualify", "order")
    ):
        return None
    if any(n.find_ancestor(exp.Select) is inner for n in inner.find_all(exp.AggFunc, exp.Window)):
        return None
    dropped = _drop_join(inner)
    if dropped is None:
        return None
    copy = select.copy()
    (copy.args.get("from_") or copy.args.get("from")).this.set("this", dropped)
    return copy


def _set_former(select: exp.Expression) -> bool:
    """A select that only removes duplicates: ``DISTINCT``, or ``GROUP BY`` with no aggregate or HAVING."""

    if not isinstance(select, exp.Select) or any(select.args.get(k) for k in ("having", "limit", "offset", "qualify")):
        return False
    if any(n.find_ancestor(exp.Select) is select for n in select.find_all(exp.AggFunc, exp.Window)):
        return False
    distinct = select.args.get("distinct")
    if distinct is not None:
        return not distinct.args.get("on") and not select.args.get("group")
    group = select.args.get("group")
    return bool(group) and not any(group.args.get(k) for k in ("grouping_sets", "cube", "rollup", "totals"))


def _strip_set_formers(select: exp.Select) -> bool:
    changed = False
    source = select.args.get("from_") or select.args.get("from")
    items = ([source.this] if source is not None else []) + [j.this for j in select.args.get("joins") or []]
    for item in items:
        if isinstance(item, exp.Subquery) and _set_former(item.this):
            item.this.set("distinct", None)
            item.this.set("group", None)
            changed = True
    return changed


def strip_distinct_sources(select: exp.Select) -> exp.Expression | None:
    """Under a duplicate-blind select, a derived table that only removes duplicates need not.

    Repeating a row of a joined or filtered input only repeats output rows, which ``DISTINCT`` (or a
    grouping with duplicate-blind aggregates) removes again. Derived tables read directly, or through
    one derived table that only projects, filters and joins, are rewritten.
    """

    if not _duplicate_blind(select):
        return None
    copy = select.copy()
    changed = _strip_set_formers(copy)
    source = copy.args.get("from_") or copy.args.get("from")
    if source is not None and not copy.args.get("joins") and isinstance(source.this, exp.Subquery):
        inner = source.this.this
        if isinstance(inner, exp.Select) and not any(
            inner.args.get(k) for k in ("group", "having", "distinct", "limit", "offset", "qualify", "order")
        ) and not any(n.find_ancestor(exp.Select) is inner for n in inner.find_all(exp.AggFunc, exp.Window)):
            changed = _strip_set_formers(inner) or changed
    return copy if changed else None
