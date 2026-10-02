"""Regroup outputs that combine several aggregates (``SUM(a) / SUM(b)``).

The regrouping rules in :mod:`kumosql.algebraic_equivalence` (``_collapse_aggregate``,
``_roll_up_aggregate``, ``_regroup_distinct``) fold an aggregate over an already
grouped derived table back into one grouped query, but only when every output is a
single aggregate or a key. A ratio such as ``SUM(total) / SUM(n_amount)`` over a
finer-grained rollup is the same ratio of the folded aggregates, since each
aggregate is folded on its own and the arithmetic is applied per output row after
grouping. This rule splits such outputs into their aggregates, lets an existing
regrouping rule fold them, and puts the arithmetic back.

``SELECT region, SUM(total) / SUM(n_amount) FROM (SELECT region, status,
SUM(amount) AS total, COUNT(amount) AS n_amount FROM orders GROUP BY region,
status) GROUP BY region`` becomes ``SELECT region, SUM(amount) / COUNT(amount)
FROM orders GROUP BY region``, which the SMT prover matches with ``AVG(amount)``.
"""

from __future__ import annotations

from typing import Callable

from sqlglot import exp

_LEAF = "kq_leaf_"


def _scoped_aggregates(expr: exp.Expression, select: exp.Select) -> list[exp.AggFunc]:
    return [a for a in expr.find_all(exp.AggFunc) if a.find_ancestor(exp.Select) is select and a.find_ancestor(exp.AggFunc) is None]


def regroup_arithmetic(select: exp.Select, regroupers: tuple[Callable[[exp.Select], exp.Expression | None], ...]) -> exp.Expression | None:
    """Fold a regrouping whose outputs are arithmetic over aggregates; ``None`` if no regrouper applies."""

    if not isinstance(select, exp.Select) or any(select.find_all(exp.Window)):
        return None
    split = select.copy()
    items: list[exp.Expression] = []
    templates: list[tuple[exp.Expression, list[str]] | None] = []
    leaves = 0
    for item in split.expressions:
        expr = item.this if isinstance(item, exp.Alias) else item
        aggregates = _scoped_aggregates(expr, split)
        if not aggregates or (len(aggregates) == 1 and aggregates[0] is expr):
            templates.append(None)
            items.append(item)
            continue
        # Only arithmetic over the aggregates: anything else outside them (a column, a
        # subquery, a function call) would read values the folded query no longer has.
        for node in expr.walk():
            if isinstance(node, exp.AggFunc) and node in aggregates:
                continue
            if any(node.find_ancestor(exp.AggFunc) is a for a in aggregates):
                continue
            if not isinstance(node, (exp.Add, exp.Sub, exp.Mul, exp.Div, exp.Paren, exp.Neg, exp.Literal)):
                return None
        template = expr.copy()
        names = []
        template_aggregates = [a for a in template.find_all(exp.AggFunc) if a.find_ancestor(exp.AggFunc) is None]
        if len(template_aggregates) != len(aggregates):
            return None
        for original, placeholder in zip(aggregates, template_aggregates):
            name = f"{_LEAF}{leaves}"
            leaves += 1
            names.append(name)
            items.append(exp.alias_(original.copy(), name))
            placeholder.replace(exp.column(name))
        templates.append((template, names))
    if leaves == 0:
        return None
    split.set("expressions", items)
    for regroup in regroupers:
        folded = regroup(split)
        if folded is None or not isinstance(folded, exp.Select) or len(folded.expressions) != len(items):
            continue
        outputs = list(folded.expressions)
        by_name = {}
        for out in outputs:
            if isinstance(out, exp.Alias) and out.alias.startswith(_LEAF):
                by_name[out.alias] = out.this
        rebuilt: list[exp.Expression] = []
        position = 0
        ok = True
        for original, plan in zip(select.expressions, templates):
            if plan is None:
                rebuilt.append(outputs[position])
                position += 1
                continue
            template, names = plan
            if any(n not in by_name for n in names):
                ok = False
                break
            expr = template.copy()
            for column in list(expr.find_all(exp.Column)):
                if column.name in names:
                    column.replace(by_name[column.name].copy())
            name = original.alias_or_name if isinstance(original, exp.Alias) else ""
            rebuilt.append(exp.alias_(expr, name) if name else expr)
            position += len(names)
        if not ok:
            continue
        folded = folded.copy()
        folded.set("expressions", rebuilt)
        return folded
    return None
