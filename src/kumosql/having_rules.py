"""Move a ``HAVING`` condition on grouping keys alone into ``WHERE``.

``SELECT k, SUM(x) FROM t GROUP BY k HAVING k > 5`` is ``SELECT k, SUM(x) FROM t WHERE k > 5 GROUP BY k``:
every row of a group carries the group's key, so keeping the groups whose key passes keeps exactly the
groups of the rows that pass. Needs at least one key, since a global aggregate returns its row even when
``WHERE`` drops every input row.
"""

from __future__ import annotations

from sqlglot import exp


def key_having_to_where(select: exp.Select) -> exp.Select | None:
    """The select with each key-only conjunct of its ``HAVING`` moved to ``WHERE``, or None."""

    group, having = select.args.get("group"), select.args.get("having")
    if group is None or having is None or not group.expressions:
        return None
    if any(group.args.get(k) for k in ("rollup", "cube", "grouping_sets", "totals", "all")):
        return None
    if any(isinstance(k, (exp.Rollup, exp.Cube, exp.GroupingSets)) for k in group.expressions):
        return None
    if any(select.args.get(k) for k in ("qualify", "windows", "with_", "with")) or select.find(exp.Window):
        return None
    keys = {k.sql().lower() for k in group.expressions if isinstance(k, exp.Column)}
    # a select alias that shadows a key's name would make the name mean something else in HAVING
    shadowed = {
        item.alias.lower() for item in select.expressions
        if isinstance(item, exp.Alias) and not (isinstance(item.this, exp.Column) and item.this.name.lower() == item.alias.lower())
    }
    moved, kept = [], []
    for conjunct in _conjuncts(having.this):
        columns = list(conjunct.find_all(exp.Column))
        if (
            columns
            and all(c.sql().lower() in keys and c.name.lower() not in shadowed for c in columns)
            and not conjunct.find(exp.AggFunc, exp.Subquery, exp.Select, exp.Exists, exp.Rand, exp.Anonymous)
        ):
            moved.append(conjunct.copy())
        else:
            kept.append(conjunct.copy())
    if not moved:
        return None
    result = select.copy()
    where = result.args.get("where")
    condition = exp.and_(*([where.this] if where is not None else []), *moved)
    result.set("where", exp.Where(this=condition))
    result.set("having", exp.Having(this=exp.and_(*kept)) if kept else None)
    return result


def _conjuncts(node: exp.Expression) -> list[exp.Expression]:
    while isinstance(node, exp.Paren):
        node = node.this
    if isinstance(node, exp.And):
        return _conjuncts(node.this) + _conjuncts(node.expression)
    return [node]
