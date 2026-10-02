"""Move computed columns of a derived outer join up into the grouped select that reads it.

``SELECT f(d.x), COUNT(*) FROM (SELECT g(a.y) AS x FROM a LEFT JOIN b ON ..) AS d GROUP BY f(d.x)`` is
``SELECT f(g(d.y)), COUNT(*) FROM (SELECT a.y AS y FROM a LEFT JOIN b ON ..) AS d GROUP BY f(g(d.y))``:
the derived table only projects its joined rows, one output row per row, so computing ``g`` above it
changes nothing. Both spellings of a pushed-down projection then meet in one form, the derived table
passing bare columns. Calcite's ``ProjectJoinTransposeRule`` tests move projections the other way.
The prover reads an aggregate over an outer join only through such a derived table
(``algebraic_equivalence._wrap_outer_join_aggregate``), so the join stays inside it.
"""

from __future__ import annotations

import itertools

from sqlglot import exp

_EXTRAS = ("distinct", "group", "having", "order", "limit", "offset", "qualify", "windows", "with_", "with", "laterals", "pivots")
_counter = itertools.count()


def _deterministic(node: exp.Expression) -> bool:
    return not any(isinstance(n, (exp.Rand, exp.Anonymous, exp.AggFunc, exp.Window, exp.Subquery, exp.Exists, exp.Select, exp.Star)) for n in node.walk())


def _inside(node: exp.Expression, ancestor: exp.Expression) -> bool:
    while node is not None:
        if node is ancestor:
            return True
        node = node.parent
    return False


def lift_derived_expressions(select: exp.Select) -> exp.Expression | None:
    from .eager_aggregation import _own_aggregates

    if not (select.args.get("group") or _own_aggregates(select)) or select.args.get("joins") or select.args.get("laterals"):
        return None
    from_ = select.args.get("from_") or select.args.get("from")
    source = from_.this if from_ is not None else None
    if not isinstance(source, exp.Subquery) or not source.alias or not isinstance(source.this, exp.Select):
        return None
    inner = source.this
    joins = inner.args.get("joins") or []
    if not any(j.args.get("side") for j in joins) or any(inner.args.get(k) for k in _EXTRAS):
        return None
    if (source.args.get("alias") and source.args["alias"].args.get("columns")) or any(
        isinstance(n, (exp.Subquery, exp.Exists)) for n in select.walk() if n is not source and not _inside(n, source)
    ):
        return None
    computed = {}
    for item in inner.expressions:
        value = item.this if isinstance(item, exp.Alias) else item
        name = item.alias_or_name.lower()
        if not name or not isinstance(item, (exp.Alias, exp.Column)):
            return None
        if not isinstance(value, exp.Column):
            if not _deterministic(value) or any(not c.table for c in value.find_all(exp.Column)):
                return None
            computed[name] = value
    if not computed:
        return None
    alias = source.alias.lower()
    own = [c for c in select.find_all(exp.Column) if not _inside(c, source)]
    if any(not isinstance(c.this, exp.Star) and c.table.lower() != alias for c in own):
        return None  # bare or foreign references: leave them to the other rules
    if not any(c.name.lower() in computed for c in own):
        return None
    lifted = select.copy()
    new_inner = lifted.args.get("from_") or lifted.args.get("from")
    new_inner = new_inner.this.this
    passed = {}  # (table, column) -> output name in the derived table
    for item in new_inner.expressions:
        value = item.this if isinstance(item, exp.Alias) else item
        if isinstance(value, exp.Column):
            passed.setdefault((value.table.lower(), value.name.lower()), item.alias_or_name.lower())
    extra = []

    def output_for(column: exp.Column) -> str:
        key = (column.table.lower(), column.name.lower())
        if key not in passed:
            name = f"kumosql_l{next(_counter)}"
            passed[key] = name
            extra.append(exp.alias_(column.copy(), name))
        return passed[key]

    replacements = {}
    for name, value in computed.items():
        body = value.copy()
        for column in list(body.find_all(exp.Column)):
            column.replace(exp.column(output_for(column), table=alias))
        replacements[name] = body
    for column in [c for c in lifted.find_all(exp.Column) if not _inside(c, lifted.args.get("from_") or lifted.args.get("from"))]:
        if isinstance(column.this, exp.Star) or column.name.lower() not in replacements:
            continue
        body = replacements[column.name.lower()].copy()
        if column.parent is lifted and column.arg_key == "expressions":
            column.replace(exp.alias_(body, column.name))
        else:
            column.replace(exp.Paren(this=body) if not isinstance(body, (exp.Column, exp.Literal, exp.Func)) else body)
    new_inner.set("expressions", list(new_inner.expressions) + extra)
    return lifted
