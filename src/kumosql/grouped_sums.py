"""``COALESCE(SUM(x), 0)`` in a grouped select is ``SUM(x)`` when ``x`` is never NULL.

Every group of a ``GROUP BY`` has at least one row, so its ``SUM`` of a never-NULL column is
never NULL either. Calcite writes the coalesce when it pushes a ``COUNT`` through a ``UNION ALL``
(``SUM`` of the per-branch counts); the counts are never NULL. A derived column counts as never
NULL when every branch computes it with ``COUNT``, a non-NULL literal, or a NOT NULL table column.
Grouping sets, ROLLUP and CUBE are left alone: their grand-total group exists even without rows.
"""

from __future__ import annotations

from sqlglot import exp


def _branches(node: exp.Expression) -> list[exp.Expression]:
    while isinstance(node, exp.Subquery) and not node.alias:
        node = node.this
    if isinstance(node, exp.Union) and not node.args.get("by_name"):
        return _branches(node.this) + _branches(node.expression)
    return [node]


def _never_null(value: exp.Expression, select: exp.Select, not_null: dict[str, frozenset[str]]) -> bool:
    value = value.this if isinstance(value, exp.Alias) else value
    while isinstance(value, exp.Paren):
        value = value.this
    if isinstance(value, exp.Count):
        return True
    if isinstance(value, exp.Literal):
        return True
    if isinstance(value, exp.Column) and not isinstance(value.this, exp.Star):
        sources = [select.args.get("from_") or select.args.get("from")]
        tables = [s.this for s in sources if s is not None] + [j.this for j in select.args.get("joins") or []]
        if select.args.get("joins"):
            return False  # an outer join could pad the column with NULLs
        for table in tables:
            if isinstance(table, exp.Table) and (table.alias_or_name or "").lower() == value.table.lower():
                columns = {c.lower() for c in not_null.get(table.name, not_null.get(table.name.lower(), frozenset()))}
                return value.name.lower() in columns
    return False


def _column_never_null(source: exp.Subquery, name: str, not_null) -> bool:
    for branch in _branches(source.this):
        if not isinstance(branch, exp.Select):
            return False
        items = [item for item in branch.expressions if item.alias_or_name.lower() == name]
        if len(items) != 1 or not _never_null(items[0], branch, not_null):
            return False
    return True


def drop_grouped_sum_coalesce(select: exp.Select, not_null: dict[str, frozenset[str]]) -> exp.Expression | None:
    group = select.args.get("group")
    if group is None or not group.expressions or any(group.args.get(k) for k in ("rollup", "cube", "grouping_sets", "totals")):
        return None
    select = select.copy()
    from_ = select.args.get("from_") or select.args.get("from")
    source = from_.this if from_ is not None else None
    if not isinstance(source, exp.Subquery) or not source.alias or select.args.get("joins"):
        return None
    alias = source.alias.lower()
    targets = []
    for node in select.find_all(exp.Coalesce):
        if node.find_ancestor(exp.Select) is not select or len(node.expressions) != 1 or not isinstance(node.expressions[0], exp.Literal):
            continue
        total = node.this
        if not isinstance(total, exp.Sum) or total.args.get("distinct") or not isinstance(total.this, exp.Column):
            continue
        column = total.this
        if column.table.lower() not in ("", alias) or not _column_never_null(source, column.name.lower(), not_null):
            continue
        targets.append(node)
    if not targets:
        return None
    for node in targets:
        node.replace(node.this.copy())
    return select
