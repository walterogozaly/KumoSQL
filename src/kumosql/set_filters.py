"""Duplicate-removing set operations as filters and EXISTS tests.

Two rewrites of ``UNION``, ``INTERSECT`` and ``EXCEPT`` (the duplicate-removing forms, not ``ALL``):

* ``merge_same_source``: both operands select the same expressions from the same single table and
  differ only in their filters ``p`` and ``q``. Then ``UNION`` is ``SELECT DISTINCT .. WHERE p OR q``.
  When ``q`` reads only projected columns, a row's ``q`` depends only on its output values, so
  ``INTERSECT`` is ``.. WHERE p AND q`` and ``EXCEPT`` is ``.. WHERE p AND NOT COALESCE(q, FALSE)``
  (for ``INTERSECT`` it is enough that either filter reads only projected columns). Calcite's
  ``UnionToFilterRule``, ``IntersectToFilterRule`` and ``MinusToFilterRule`` do this.
* ``set_operation_to_exists``: ``A INTERSECT B`` is ``SELECT DISTINCT a.* FROM (A) a WHERE EXISTS
  (SELECT 1 FROM (B) b WHERE a.c1 <=> b.d1 AND ..)``, and ``EXCEPT`` is the same with ``NOT EXISTS``;
  set operations compare rows with NULLs equal, which ``<=>`` spells. It only fires once both operands
  read plain tables, so that ``merge_same_source`` gets the first chance.
"""

from __future__ import annotations

import itertools

from sqlglot import exp

from .empty_rules import is_empty

_counter = itertools.count()
_BLOCKERS = ("group", "having", "order", "limit", "offset", "qualify", "windows", "with_", "with", "laterals", "pivots")


def _unwrap(node: exp.Expression) -> exp.Expression:
    while isinstance(node, (exp.Subquery, exp.Paren)) and not node.alias:
        node = node.this
    return node


def _operand(node: exp.Expression) -> exp.Select | None:
    node = _unwrap(node)
    return node if isinstance(node, exp.Select) else None


def _from(select: exp.Select) -> exp.Expression | None:
    from_ = select.args.get("from_") or select.args.get("from")
    return from_.this if from_ is not None else None


def _deterministic(node: exp.Expression) -> bool:
    return not any(isinstance(n, (exp.Rand, exp.Anonymous, exp.AggFunc, exp.Window, exp.Subquery, exp.Exists, exp.Placeholder)) for n in node.walk())


def _flatten(select: exp.Select) -> exp.Select | None:
    """``SELECT f(d.x) FROM (SELECT g(t.y) AS x FROM t WHERE w) AS d WHERE h(d.x)`` read as one select of ``t``."""

    while True:
        source = _from(select)
        if not isinstance(source, exp.Subquery) or not source.alias:
            return select
        inner = source.this
        if not isinstance(inner, exp.Select) or select.args.get("joins") or any(select.args.get(k) for k in _BLOCKERS):
            return None
        if inner.args.get("distinct") or any(inner.args.get(k) for k in _BLOCKERS):
            return None
        if any(isinstance(n, (exp.Subquery, exp.Exists)) and not _within(n, source) for n in select.walk()):
            return None
        mapping = {}
        for item in inner.expressions:
            name = item.alias_or_name.lower()
            if not name or name in mapping or isinstance(_value(item), exp.Star) or not _deterministic(_value(item)):
                return None
            mapping[name] = _value(item)
        alias = source.alias.lower()
        flat = select.copy()
        flat.set("from_", None)
        flat.set("from", None)
        flat_where = flat.args.get("where")
        for column in list(flat.find_all(exp.Column)):
            if column.table.lower() not in ("", alias) or column.name.lower() not in mapping:
                return None
            value = mapping[column.name.lower()].copy()
            column.replace(value if isinstance(value, (exp.Column, exp.Literal)) else exp.Paren(this=value))
        for item in flat.expressions:
            if not isinstance(item, exp.Alias) and not (isinstance(item, exp.Column)):
                return None
        flat.set("expressions", [item if isinstance(item, exp.Alias) or item.alias_or_name.lower() == original.alias_or_name.lower() else exp.alias_(item, original.alias_or_name) for item, original in zip(flat.expressions, select.expressions)])
        inner_from = inner.args.get("from_") or inner.args.get("from")
        if inner_from is None:
            return None
        flat.set("from_", inner_from.copy())
        flat.set("joins", [j.copy() for j in inner.args.get("joins") or []] or None)
        parts = [w.this.copy() for w in (inner.args.get("where"), flat_where) if w is not None]
        flat.set("where", exp.Where(this=exp.and_(*[exp.Paren(this=p) for p in parts])) if parts else None)
        select = flat


def _within(node: exp.Expression, ancestor: exp.Expression) -> bool:
    while node is not None:
        if node is ancestor:
            return True
        node = node.parent
    return False


def _single_table(select: exp.Select):
    """``(table, alias, projections, where)`` for a projection and filter of one table, else None."""

    if any(select.args.get(k) for k in _BLOCKERS) or select.args.get("joins"):
        return None
    table = _from(select)
    if not isinstance(table, exp.Table) or table.args.get("joins"):
        return None
    alias = (table.alias_or_name or "").lower()
    where = select.args.get("where")
    parts = list(select.expressions) + ([where.this] if where is not None else [])
    for part in parts:
        if not _deterministic(part) or isinstance(part, exp.Star):
            return None
        if any(c.table.lower() not in ("", alias) or isinstance(c.this, exp.Star) for c in part.find_all(exp.Column)):
            return None
    return table, alias, select.expressions, where.this if where is not None else None


def _rename(node: exp.Expression, old: str, new: str) -> exp.Expression:
    node = node.copy()
    for column in node.find_all(exp.Column):
        if column.table.lower() in ("", old):
            column.set("table", exp.to_identifier(new))
    return node


def _value(item: exp.Expression) -> exp.Expression:
    return item.this if isinstance(item, exp.Alias) else item


def _reads_only(condition: exp.Expression | None, projected: set[str]) -> bool:
    if condition is None:
        return True
    return all(c.name.lower() in projected for c in condition.find_all(exp.Column))


def merge_same_source(node: exp.Expression) -> exp.Expression | None:
    if not isinstance(node, (exp.Union, exp.Intersect, exp.Except)) or not node.args.get("distinct", True):
        return None
    if isinstance(node, exp.Union) and (node.find_ancestor(exp.SetOperation) is not None or any(isinstance(_unwrap(o), exp.SetOperation) for o in (node.this, node.expression))):
        return None  # a chain of unions is flattened into one n-ary union instead, on both sides alike
    left, right = _operand(node.this), _operand(node.expression)
    left, right = left and _flatten(left), right and _flatten(right)
    if left is None or right is None:
        return None
    a, b = _single_table(left), _single_table(right)
    if a is None or b is None:
        return None
    (a_table, a_alias, a_items, p), (b_table, b_alias, b_items, q) = a, b
    if a_table.name.lower() != b_table.name.lower() or a_table.args.get("db") != b_table.args.get("db") or len(a_items) != len(b_items):
        return None
    target = a_table.alias_or_name
    if [_rename(_value(i), a_alias, target).sql() for i in a_items] != [_rename(_value(i), b_alias, target).sql() for i in b_items]:
        return None
    q = _rename(q, b_alias, target) if q is not None else None
    projected = {_value(i).name.lower() for i in a_items if isinstance(_value(i), exp.Column)}
    if isinstance(node, exp.Union):
        if p is None or q is None:
            condition = None
        else:
            condition = exp.Or(this=exp.Paren(this=p.copy()), expression=exp.Paren(this=q))
    elif isinstance(node, exp.Intersect):
        if not (_reads_only(p, projected) or _reads_only(q, projected)):
            return None
        parts = [exp.Paren(this=c.copy()) for c in (p, q) if c is not None]
        condition = exp.and_(*parts) if parts else None
    else:
        if not _reads_only(q, projected):
            return None
        rejected = exp.false() if q is None else exp.Not(this=exp.Coalesce(this=exp.Paren(this=q), expressions=[exp.false()]))
        condition = exp.and_(exp.Paren(this=p.copy()), rejected) if p is not None else rejected
    merged = left.copy()
    merged.set("where", exp.Where(this=condition) if condition is not None else None)
    merged.set("distinct", exp.Distinct())
    return merged


def _plain(select: exp.Select) -> bool:
    sources = [_from(select)] + [j.this for j in select.args.get("joins") or []]
    return all(isinstance(s, exp.Table) for s in sources)


def _names(select: exp.Select) -> list[str] | None:
    names = [item.alias_or_name.lower() for item in select.expressions]
    if any(not n or n == "*" for n in names) or len(set(names)) != len(names):
        return None
    return names


def set_operation_to_exists(node: exp.Expression) -> exp.Expression | None:
    if not isinstance(node, (exp.Intersect, exp.Except)) or not node.args.get("distinct", True):
        return None
    left, right = _operand(node.this), _operand(node.expression)
    if left is None or right is None or not all(f is not None and _plain(f) for f in (_flatten(left), _flatten(right))):
        return None
    if is_empty(left) or is_empty(right):  # left to the empty-operand folding
        return None
    a, b = _names(left), _names(right)
    if a is None or b is None or len(a) != len(b):
        return None
    n = next(_counter)
    outer, inner = f"kumosql_s{n}", f"kumosql_r{n}"
    match = exp.and_(*[exp.NullSafeEQ(this=exp.column(x, table=outer), expression=exp.column(y, table=inner)) for x, y in zip(a, b)])
    probe = exp.select("1").from_(exp.Subquery(this=right.copy(), alias=exp.TableAlias(this=exp.to_identifier(inner)))).where(match)
    test: exp.Expression = exp.Exists(this=probe)
    if isinstance(node, exp.Except):
        test = exp.Not(this=test)
    out = exp.select(*[exp.alias_(exp.column(x, table=outer), x) for x in a]).from_(
        exp.Subquery(this=left.copy(), alias=exp.TableAlias(this=exp.to_identifier(outer)))
    ).where(test)
    out.set("distinct", exp.Distinct())
    return out
