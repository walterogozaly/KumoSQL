"""Read an inner join that only filters, under ``DISTINCT``, as an ``EXISTS`` test.

``SELECT DISTINCT t.a FROM t JOIN u ON t.a = u.b`` repeats a row of ``t`` once per
partner in ``u``; when nothing but the join condition (and ``WHERE`` conjuncts)
reads ``u``, the repeats are all the join adds, and a query that cannot see
repeats (``DISTINCT``, or ``GROUP BY`` whose aggregates are all ``MIN``/``MAX``/
``DISTINCT`` ones) returns the same rows as
``SELECT DISTINCT t.a FROM t WHERE EXISTS (SELECT 1 FROM u WHERE t.a = u.b)``.

The prover compares ``EXISTS``/``IN`` tests well and duplicate-producing joins
poorly, so ``prove_equivalent_algebraic`` tries this reading on both queries when
the first attempt finds no proof. Only inner and cross joins of a table or derived
table qualify, every column of the select must name its table, and no ``RIGHT``
or ``FULL`` join may sit in the same ``FROM`` (it would keep rows the test drops).
"""

from __future__ import annotations

import sqlglot
from sqlglot import exp

from .dedup_join_rules import _duplicate_blind, _inside, _name


def _conjuncts(condition: exp.Expression | None) -> list[exp.Expression]:
    if condition is None:
        return []
    while isinstance(condition, exp.Paren):
        condition = condition.this
    if isinstance(condition, exp.And):
        return _conjuncts(condition.this) + _conjuncts(condition.expression)
    return [condition]


def _reads(node: exp.Expression, name: str) -> bool:
    return any(c.table.lower() == name for c in node.find_all(exp.Column))


def _filtering_join(select: exp.Select, index: int) -> exp.Select | None:
    joins = select.args["joins"]
    join = joins[index]
    kind = (join.args.get("kind") or "").upper()
    if join.args.get("side") or kind not in ("", "INNER", "CROSS") or join.args.get("using") or join.args.get("method"):
        return None
    source = join.this
    if not isinstance(source, (exp.Table, exp.Subquery)) or (isinstance(source, exp.Table) and isinstance(source.this, exp.Func)):
        return None
    name = (_name(source) or "").lower()
    if not name:
        return None
    others = [s for s in _sources(select) if s is not source]
    if any((_name(s) or "").lower() == name for s in others):
        return None
    # the alias must mean this source everywhere it is read
    for node in select.walk():
        if node is source or _inside(node, source):
            continue
        if isinstance(node, (exp.Table, exp.Subquery)) and node is not source and (_name(node) or "").lower() == name:
            return None
    on = join.args.get("on")
    where = select.args.get("where")
    moved = [c for c in _conjuncts(where.this if where else None) if _reads(c, name)]
    kept = [c for c in _conjuncts(where.this if where else None) if not _reads(c, name)]
    for column in select.find_all(exp.Column):
        if column.table.lower() != name or _inside(column, source):
            continue
        if on is not None and _inside(column, on):
            continue
        if any(_inside(column, c) for c in moved):
            continue
        return None
    test = exp.select("1").from_(source.copy())
    conditions = ([on.copy()] if on is not None else []) + [c.copy() for c in moved]
    if conditions:
        test = test.where(exp.and_(*conditions))
    copy = select.copy()
    remaining = [j for i, j in enumerate(copy.args["joins"]) if i != index]
    copy.set("joins", remaining or None)
    copy.set("where", exp.Where(this=exp.and_(*[c.copy() for c in kept], exp.Exists(this=test))))
    return copy


def _sources(select: exp.Select) -> list[exp.Expression]:
    from_ = select.args.get("from_") or select.args.get("from")
    return ([from_.this] if from_ is not None else []) + [j.this for j in select.args.get("joins") or []]


def join_to_exists(select: exp.Select) -> exp.Select | None:
    """``select`` with one filtering inner join read as an ``EXISTS`` test, or ``None``."""

    joins = select.args.get("joins") or []
    if not joins or select.args.get("laterals") or not _duplicate_blind(select):
        return None
    if any((j.args.get("side") or "").upper() in ("RIGHT", "FULL") or (j.args.get("kind") or "").upper() == "OUTER" for j in joins):
        return None
    if any(isinstance(s, exp.Star) for s in select.expressions) or any(
        isinstance(c.this, exp.Star) for c in select.find_all(exp.Column)
    ):
        return None
    if any(not c.table for c in select.find_all(exp.Column)):
        return None
    for index in reversed(range(len(joins))):
        rewritten = _filtering_join(select, index)
        if rewritten is not None:
            return rewritten
    return None


def semijoin_reading(sql: str, dialect: str = "bigquery") -> str | None:
    """``sql`` with every filtering inner join under a duplicate-blind select read as ``EXISTS``,
    or ``None`` when there is none."""

    try:
        tree = sqlglot.parse_one(sql, read=dialect)
    except sqlglot.errors.SqlglotError:
        return None
    changed = False

    def step(node: exp.Expression) -> exp.Expression:
        nonlocal changed
        if isinstance(node, exp.Select):
            while True:
                rewritten = join_to_exists(node)
                if rewritten is None:
                    break
                node, changed = rewritten, True
        return node

    tree = tree.transform(step)
    return tree.sql(dialect=dialect) if changed else None
