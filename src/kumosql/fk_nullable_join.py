"""Drop a foreign-key join whose child column is nullable, keeping ``col IS NOT NULL``.

``SELECT o.id FROM orders o JOIN customers c ON o.customer_id = c.id`` is
``SELECT id FROM orders WHERE customer_id IS NOT NULL`` when ``orders(customer_id)`` references
the unique key ``customers(id)``: an equality join already loses the rows whose ``customer_id`` is
NULL, and every other row finds exactly one parent. ``fk_rules.drop_fk_join`` needs the child column
declared NOT NULL, or a ``WHERE`` conjunct that says so. The equality in the join condition says so too,
so this rule writes that conjunct down (for the join columns of every inner join) and lets ``drop_fk_join``
decide; the select is returned only when that drops a join, never otherwise.
"""

from __future__ import annotations

from sqlglot import exp

from .ast_utils import conjuncts as _conjuncts
from .fk_rules import drop_fk_join


def drop_nullable_fk_join(select: exp.Select, keys, not_null, foreign_keys) -> exp.Expression | None:
    if not foreign_keys or not select.args.get("joins"):
        return None
    probe = select.copy()
    where = probe.args.get("where")
    present = {part.sql() for part in _conjuncts(where.this)} if where is not None else set()
    added = False
    for join in probe.args["joins"]:
        if join.args.get("side") or (join.args.get("kind") or "").upper() not in ("", "INNER") or join.args.get("on") is None:
            continue
        for part in _conjuncts(join.args["on"]):
            if not isinstance(part, exp.EQ):
                continue
            for column in (part.this, part.expression):
                if not isinstance(column, exp.Column) or not column.table:
                    continue
                condition = exp.Not(this=exp.Is(this=column.copy(), expression=exp.Null()))
                if condition.sql() not in present:
                    present.add(condition.sql())
                    probe.where(condition, copy=False)
                    added = True
    return drop_fk_join(probe, keys, not_null, foreign_keys) if added else None
