"""An uncorrelated EXISTS in a LEFT JOIN's ON clause filters the padded side.

``a LEFT JOIN t AS d ON c AND EXISTS (q)`` is ``a LEFT JOIN (SELECT d.x, .. FROM t AS d WHERE EXISTS (q)) AS d ON c``
when ``q`` reads nothing of ``a`` or ``d``. The EXISTS is one truth value of the database state, the same
for every pair of rows, so for each row of ``a`` the bag of matching rows of ``t`` is the same either way:
all rows of ``t`` that satisfy ``c`` when ``q`` has a row, none when it has not, and a row of ``a`` with no
match is padded with NULLs in both forms. ``NOT EXISTS (q)`` is moved the same way. EXISTS is never NULL.

This brings the shape Calcite produces for an EXISTS in a join condition (the padded side inner-joined to
``SELECT TRUE FROM q GROUP BY TRUE``, which other rules already read as a ``WHERE EXISTS (q)`` filter on
that side) and the original ON clause to one form. The moved filter goes into a derived table rather than
the other way round because ``_merge_outer_right_filter`` leaves a filter with a subquery where it is.
"""

from __future__ import annotations

from sqlglot import exp

from .ast_utils import conjuncts
from .scalar_subqueries import is_uncorrelated


def _constant_exists(part: exp.Expression, schema: dict[str, list[str]]) -> bool:
    node = part.this if isinstance(part, exp.Not) else part
    while isinstance(node, exp.Paren):
        node = node.this
    if not isinstance(node, exp.Exists) or not isinstance(node.this, exp.Select):
        return False
    return is_uncorrelated(exp.Subquery(this=node.this.copy()), schema)


def _table_columns(table: exp.Table, schema: dict[str, list[str]]) -> list[str] | None:
    if table.args.get("db") or table.args.get("catalog"):
        return None
    lowered = {key.lower(): cols for key, cols in schema.items()}
    columns = lowered.get(table.name.lower())
    return [c.lower() for c in columns] if columns else None


def move_exists_into_padded_side(select: exp.Select, schema: dict[str, list[str]] | None) -> exp.Expression | None:
    if not schema:
        return None
    for index, join in enumerate(select.args.get("joins") or []):
        on = join.args.get("on")
        if (join.side or "").upper() != "LEFT" or join.args.get("kind") or on is None:
            continue
        table = join.this
        if not isinstance(table, exp.Table) or not table.alias_or_name or any(table.args.get(k) for k in ("joins", "pivots", "laterals", "version", "hints")):
            continue
        columns = _table_columns(table, schema)
        if columns is None:
            continue
        parts = conjuncts(on)
        moved = [p for p in parts if _constant_exists(p, schema)]
        if not moved:
            continue
        kept = [p for p in parts if not any(p is m for m in moved)]
        alias = table.alias_or_name
        inner = exp.select(*(exp.alias_(exp.column(c, table=alias), c) for c in columns)).from_(table.copy())
        inner = inner.where(exp.and_(*(m.copy() for m in moved)), copy=False)
        copy = select.copy()
        target = copy.args["joins"][index]
        target.set("this", exp.Subquery(this=inner, alias=exp.TableAlias(this=exp.to_identifier(alias))))
        target.set("on", exp.and_(*(k.copy() for k in kept)) if kept else exp.true())
        return copy
    return None
