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

from .ast_utils import FROM_KEY, extended_grouping


_BLIND = (exp.Min, exp.Max)


def _duplicate_blind(select: exp.Select) -> bool:
    distinct = select.args.get("distinct")
    if distinct is not None and distinct.args.get("on"):
        return False
    if distinct is None and not select.args.get("group"):
        return False
    # DISTINCT dedups output rows, not what its aggregates read: ``SELECT DISTINCT COUNT(*)`` sees repeats
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


def _drop_unread_distinct_left_join(select: exp.Select) -> exp.Select | None:
    """A LEFT JOIN to a DISTINCT projection cannot repeat a left row when ON fixes every output."""

    joins = select.args.get("joins") or []
    if len(joins) != 1 or select.args.get("laterals"):
        return None
    join = joins[0]
    if (join.side or "").upper() != "LEFT" or join.args.get("kind") or join.args.get("using") or join.args.get("natural"):
        return None
    far = join.this
    name = _name(far)
    on = join.args.get("on")
    if not name or not isinstance(far, exp.Subquery) or not isinstance(far.this, exp.Select) or on is None:
        return None
    body = far.this
    distinct = body.args.get("distinct")
    if not isinstance(distinct, exp.Distinct) or distinct.args.get("on") is not None:
        return None
    if any(body.args.get(key) for key in ("group", "having", "order", "limit", "offset", "qualify", "windows", "with_", "with", "joins", "laterals")):
        return None
    from_ = body.args.get("from_") or body.args.get("from")
    if from_ is None or not isinstance(from_.this, exp.Table):
        return None
    output_names = []
    for item in body.expressions:
        value = item.this if isinstance(item, exp.Alias) else item
        output_name = (item.alias_or_name or "").lower()
        if not isinstance(value, exp.Column) or isinstance(value.this, exp.Star) or not output_name:
            return None
        output_names.append(output_name)
    if not output_names or len(set(output_names)) != len(output_names):
        return None

    alias = name.lower()
    # Limit this rule to simple deterministic projections and predicates. The relational proof below
    # is about row multiplicity; leaving expression evaluation order alone keeps its premise explicit.
    for node in select.walk():
        if _inside(node, far):
            continue
        if isinstance(node, (exp.Func, exp.Subquery, exp.Exists, exp.Window)):
            return None
    for node in body.walk():
        if isinstance(node, (exp.Func, exp.Subquery, exp.Exists, exp.Window)) and node is not body:
            return None

    for column in select.find_all(exp.Column):
        # The projected columns inside ``far`` refer to that subquery's input scope. A
        # source table there may share the join alias (for example, ``y.b`` projected
        # from ``y`` and then joined back as ``AS y``), but those references do not read
        # the outer joined row.
        if _inside(column, far):
            continue
        if column.table.lower() == alias:
            if not _inside(column, on):
                return None
        elif not column.table and column.name.lower() in output_names and not _inside(column, on):
            return None

    fixed_outputs = set()

    def conjuncts(node: exp.Expression) -> list[exp.Expression]:
        if isinstance(node, exp.Paren) and not isinstance(node.this, exp.Or):
            return conjuncts(node.this)
        if isinstance(node, exp.And):
            return conjuncts(node.this) + conjuncts(node.expression)
        return [node]

    for part in conjuncts(on):
        if not isinstance(part, exp.EQ):
            continue
        for right, other in ((part.this, part.expression), (part.expression, part.this)):
            if (
                isinstance(right, exp.Column)
                and right.table.lower() == alias
                and right.name.lower() in output_names
                and not any(
                    column.table.lower() == alias
                    or (not column.table and column.name.lower() in output_names)
                    for column in other.find_all(exp.Column)
                )
            ):
                fixed_outputs.add(right.name.lower())
    if fixed_outputs != set(output_names):
        return None
    # All references to the joined alias have already been checked in the outer query
    # scope above. `_drop_join` also scans the derived table's own projection, where a
    # source table can coincidentally have the same name as the join alias.
    copy = select.copy()
    near = (copy.args.get("from_") or copy.args.get("from")).this
    copy.set("joins", None)
    copy.set(FROM_KEY, exp.From(this=near.copy()))
    return copy


def drop_unread_outer_join(select: exp.Select) -> exp.Expression | None:
    unique = _drop_unread_distinct_left_join(select)
    if unique is not None:
        return unique
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
    return bool(group) and not extended_grouping(group)


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
