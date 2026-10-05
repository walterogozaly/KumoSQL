"""Calcite's decorrelated subquery aggregates, read back as correlated one-row aggregates.

Calcite removes a correlated aggregate subquery by grouping it on the correlation key and joining
the groups back to the outer rows. Two shapes are read here, each as
``LEFT JOIN LATERAL (SELECT <items> FROM <rows> WHERE k = o.c) ON TRUE``, which
``quantified_rules`` then folds like an uncorrelated one-row aggregate:

* ``o LEFT JOIN (... (SELECT k, aggs FROM rows GROUP BY k) ...) AS t ON o.c = t.k``: each outer
  row meets the group of its own key, or none (then every column of ``t`` is NULL). Over the rows
  with ``k = o.c``, the group exists exactly when ``COUNT(*) > 0``, so each item becomes
  ``CASE WHEN COUNT(*) = 0 THEN NULL ELSE <item> END`` (``MIN`` and ``MAX`` are NULL over no
  rows already). A NULL ``o.c`` meets no group, and no row has ``k = NULL``: both give NULL.
* The domain join of the top-down decorrelator: ``o [LEFT] JOIN (... (SELECT a FROM t GROUP BY a) AS d
  LEFT JOIN (grouped aggregate) AS g ON d.a <=> g.k ...) AS t ON o.c = t.a``, where ``o.c``
  reads column ``a`` of a real, NOT NULL row of the same table ``t``. The domain then holds
  ``o.c`` exactly once, so each outer row meets exactly one row; the NULL extension of ``g``
  happens below the projections, so only ``g``'s columns are wrapped. Further conjuncts of an
  inner join's ON move to the WHERE.

Every rewrite keeps each column's value for each outer row; ``quantified_rules`` undoes one that no
fold used.
"""

from __future__ import annotations

from sqlglot import exp

from .ast_utils import declared_key, extended_grouping, same_table
from .decorrelation_rules import _conjuncts, _real_sources, _sources

_PROJECTION_ONLY = ("where", "group", "having", "qualify", "distinct", "limit", "offset", "order", "windows", "laterals", "joins")
_GROUPED_EXTRAS = ("having", "qualify", "distinct", "limit", "offset", "order", "windows", "laterals")


def _from(select: exp.Select) -> exp.Expression | None:
    from_ = select.args.get("from_") or select.args.get("from")
    return from_.this if from_ is not None else None


def _derived(node: exp.Expression | None) -> exp.Select | None:
    return node.this if isinstance(node, exp.Subquery) and isinstance(node.this, exp.Select) and node.alias else None


def _item(select: exp.Select, name: str) -> exp.Expression | None:
    hits = [e for e in select.expressions if e.alias_or_name.lower() == name]
    return hits[0].unalias() if len(hits) == 1 else None


def _projection(select: exp.Select) -> bool:
    if any(select.args.get(k) for k in _PROJECTION_ONLY) or _derived(_from(select)) is None:
        return False
    return not any(n.find_ancestor(exp.Select) is select for e in select.expressions for n in e.find_all(exp.AggFunc, exp.Window, exp.Star, exp.Subquery))


def _grouped(select: exp.Select) -> exp.Column | None:
    """The one non-constant GROUP BY column of a plain grouped aggregate."""

    group = select.args.get("group")
    if group is None or any(select.args.get(k) for k in _GROUPED_EXTRAS) or any(group.args.get(k) for k in ("rollup", "cube", "grouping_sets")):
        return None
    keys = [g for g in group.expressions if not isinstance(g, (exp.Boolean, exp.Literal))]
    if len(keys) != 1 or not isinstance(keys[0], exp.Column) or any(isinstance(g, exp.Null) for g in group.expressions):
        return None
    return keys[0]


def _value(select: exp.Select, name: str) -> exp.Expression | None:
    """A grouped item read as a value over the group's rows: an aggregate or a constant; never the key."""

    value = _item(select, name)
    if isinstance(value, (exp.Min, exp.Max)) or (isinstance(value, exp.Count) and not isinstance(value.this, exp.Distinct)):
        return value
    if isinstance(value, exp.Boolean) or (isinstance(value, exp.Literal) and not value.is_string):
        return value
    return None


def _empty_null(value: exp.Expression) -> exp.Expression:
    """``value`` over the rows of a group that may be missing: NULL when there are no rows."""

    if isinstance(value, (exp.Min, exp.Max)):
        return value.copy()
    return exp.Case(
        ifs=[exp.If(this=exp.EQ(this=exp.Count(this=exp.Star()), expression=exp.Literal.number(0)), true=exp.Null())],
        default=value.copy(),
    )


def _substitute(expr: exp.Expression, lower_alias: str, values: dict) -> exp.Expression | None:
    copy = expr.copy()
    for column in list(copy.find_all(exp.Column)):
        if column.table and column.table.lower() != lower_alias:
            return None
        value = values.get(column.name.lower())
        if value is None:
            return None
        replacement = value.copy()
        if not isinstance(replacement, (exp.Column, exp.Literal, exp.Boolean, exp.Null, exp.AggFunc, exp.Paren)):
            replacement = exp.Paren(this=replacement)
        if column is copy:
            return replacement
        column.replace(replacement)
    return copy


def _unfiltered(select: exp.Select, column: exp.Expression, depth: int = 0) -> tuple[exp.Table, str] | None:
    """The table column whose every value ``column`` lists: plain projections down to one table."""

    if depth > 8 or not isinstance(column, exp.Column) or any(select.args.get(k) for k in ("where", "joins", "laterals", "having", "qualify", "limit", "offset")):
        return None
    source = _from(select)
    if isinstance(source, exp.Table):
        return source, column.name.lower()
    inner = _derived(source)
    if inner is None or column.table.lower() not in ("", source.alias.lower()):
        return None
    if inner.args.get("group") or inner.args.get("distinct"):
        return None
    return _unfiltered(inner, _item(inner, column.name.lower()), depth + 1)


def _real_column(select: exp.Select, column: exp.Expression, depth: int = 0) -> tuple[exp.Table, str] | None:
    """The table column ``column`` reads from a real row (never NULL-extended), through derived tables."""

    if depth > 8 or not isinstance(column, exp.Column):
        return None
    sources = _real_sources(select)
    if column.table:
        source = sources.get(column.table.lower())
    else:
        everything = _sources(select)
        source = everything[0] if len(everything) == 1 and not select.args.get("laterals") and sources else None
    if isinstance(source, exp.Table):
        return source, column.name.lower()
    inner = _derived(source)
    if inner is None:
        return None
    group = inner.args.get("group")
    if group is not None and extended_grouping(group):
        return None
    return _real_column(inner, _item(inner, column.name.lower()), depth + 1)


def _domain(select: exp.Select) -> tuple[exp.Table, str] | None:
    """``SELECT a FROM t GROUP BY a`` (or DISTINCT, through plain projections): every value of ``t.a`` once."""

    if len(select.expressions) != 1 or any(select.args.get(k) for k in ("where", "joins", "laterals", "having", "qualify", "limit", "offset", "windows")):
        return None
    value = select.expressions[0].unalias()
    group = select.args.get("group")
    if group is not None:
        if extended_grouping(group) or [g.sql() for g in group.expressions] != [value.sql()]:
            return None
    elif not (isinstance(select.args.get("distinct"), exp.Distinct) and not select.args["distinct"].args.get("on")):
        return None
    source = _from(select)
    if isinstance(source, exp.Table):
        return (source, value.name.lower()) if isinstance(value, exp.Column) else None
    inner = _derived(source)
    if inner is None or not isinstance(value, exp.Column) or value.table.lower() not in ("", source.alias.lower()):
        return None
    return _unfiltered(inner, _item(inner, value.name.lower()))


def _bottom_values(bottom: exp.Select, key_name: str, outer: exp.Column, not_null: dict, select: exp.Select):
    """(values of the bottom select's items, the rows select, whether the join may be inner) or None."""

    key = _grouped(bottom)
    if key is not None:
        if _item(bottom, key_name) is None or _item(bottom, key_name).sql() != key.sql():
            return None
        values = {e.alias_or_name.lower(): _value(bottom, e.alias_or_name.lower()) for e in bottom.expressions}
        values[key_name] = outer.copy()
        return values, bottom, key, False
    # The domain join: FROM (domain) AS d LEFT JOIN (grouped) AS g ON d.a <=> g.k.
    joins = bottom.args.get("joins") or []
    if len(joins) != 1 or any(bottom.args.get(k) for k in _PROJECTION_ONLY if k != "joins"):
        return None
    join = joins[0]
    if (join.args.get("side") or "").upper() != "LEFT" or join.args.get("kind") or join.args.get("using"):
        return None
    domain_source, grouped_source = _from(bottom), join.this
    domain, grouped = _derived(domain_source), _derived(grouped_source)
    if domain is None or grouped is None:
        return None
    d_alias, g_alias = domain_source.alias.lower(), grouped_source.alias.lower()
    on = join.args.get("on")
    while isinstance(on, exp.Paren):
        on = on.this
    if not isinstance(on, (exp.EQ, exp.NullSafeEQ)) or not all(isinstance(s, exp.Column) for s in (on.this, on.expression)):
        return None
    sides = {s.table.lower(): s.name.lower() for s in (on.this, on.expression)}
    if set(sides) != {d_alias, g_alias}:
        return None
    key = _grouped(grouped)
    if key is None or _item(grouped, sides[g_alias]) is None or _item(grouped, sides[g_alias]).sql() != key.sql():
        return None
    item = _item(bottom, key_name)
    if not isinstance(item, exp.Column) or item.table.lower() != d_alias or item.name.lower() != sides[d_alias]:
        return None
    if _item(domain, sides[d_alias]) is None:
        return None
    listed = _domain(domain)
    read = _real_column(select, outer)
    if listed is None or read is None or not same_table(listed[0], read[0]) or listed[1] != read[1]:
        return None
    if listed[1] not in not_null.get(declared_key(listed[0]), set()):
        return None
    values = {}
    for e in bottom.expressions:
        value = e.unalias()
        name = e.alias_or_name.lower()
        values[name] = outer.copy() if name == key_name else None
        if isinstance(value, exp.Column) and value.table.lower() == g_alias:
            found = _value(grouped, value.name.lower())
            if found is not None:
                values[name] = _empty_null(found)
    return values, grouped, key, True


def lateral_aggregates(select: exp.Select, not_null: dict) -> list[exp.Lateral]:
    """Rewrite the decorrelated aggregate joins of ``select`` in place; returns the LATERAL tables made."""

    made = []
    joins = select.args.get("joins") or []
    if select.args.get("laterals") or any((j.args.get("side") or "").upper() in ("RIGHT", "FULL") for j in joins):
        return made
    if any(isinstance(e, exp.Star) or isinstance(e, exp.Column) and isinstance(e.this, exp.Star) for e in select.expressions):
        return made
    for join in joins:
        side = (join.args.get("side") or "").upper()
        body, alias = _derived(join.this), (join.this.alias or "").lower()
        kind = (join.args.get("kind") or "").upper()
        if body is None or side not in ("", "LEFT") or kind not in ("", "INNER", "OUTER") or join.args.get("using") or join.args.get("method"):
            continue
        sources = _sources(select)
        position = next((i for i, s in enumerate(sources) if s is join.this), None)
        earlier = {(s.alias_or_name or "").lower(): i for i, s in enumerate(sources) if position is not None and i < position}
        # ON o.c = t.k, plus (for an inner domain join) further conjuncts.
        equality, rest = None, []
        for part in _conjuncts(join.args.get("on")):
            while isinstance(part, exp.Paren):
                part = part.this
            if equality is None and isinstance(part, exp.EQ) and all(isinstance(s, exp.Column) for s in (part.this, part.expression)):
                tables = [s.table.lower() for s in (part.this, part.expression)]
                if tables.count(alias) == 1 and all(t in earlier for t in tables if t != alias):
                    equality = part
                    continue
            rest.append(part)
        if equality is None:
            continue
        mine, outer = (equality.this, equality.expression) if equality.this.table.lower() == alias else (equality.expression, equality.this)
        key_name = mine.name.lower()
        layers = [body]
        while _projection(layers[-1]):
            layers.append(_derived(_from(layers[-1])))
        bottom, projections = layers[-1], layers[:-1]
        name = key_name
        for layer in projections:
            value = _item(layer, name)
            lower = _from(layer).alias.lower()
            if not isinstance(value, exp.Column) or value.table.lower() not in ("", lower):
                name = None
                break
            name = value.name.lower()
        if name is None:
            continue
        found = _bottom_values(bottom, name, outer, not_null, select)
        if found is None:
            continue
        values, rows, key, domain_join = found
        # A LEFT JOIN keeps every outer row only without further ON conjuncts; an inner join to the
        # groups keeps the rows whose group exists, which the LATERAL table reports as ``kumosql_found``.
        if rest and side == "LEFT":
            continue
        found_name = "kumosql_found" if not domain_join and side == "" else None
        for layer in reversed(projections):
            lower = _from(layer).alias.lower()
            values = {e.alias_or_name.lower(): _substitute(e.unalias(), lower, values) for e in layer.expressions}
        top = [(e.alias_or_name, values.get(e.alias_or_name.lower())) for e in body.expressions]
        if not top or any(v is None for _, v in top):
            continue
        if not domain_join:
            top = [(n, _empty_null(v)) for n, v in top]
        if found_name is not None:
            if any(n.lower() == found_name for n, _ in top):
                continue
            top.append((found_name, _empty_null(exp.true())))
            rest = [exp.column(found_name, table=join.this.alias)] + rest
        if any((s.alias_or_name or "").lower() == outer.table.lower() for s in rows.find_all(exp.Table, exp.Subquery, exp.Lateral)):
            continue
        lateral_body = rows.copy()
        lateral_body.set("group", None)
        lateral_body.set("expressions", [exp.alias_(v, n) for n, v in top])
        lateral_body.where(exp.EQ(this=outer.copy(), expression=key.copy()), copy=False)
        lateral = exp.Lateral(this=exp.Subquery(this=lateral_body), alias=exp.TableAlias(this=exp.to_identifier(join.this.alias)))
        join.set("this", lateral)
        join.set("side", "LEFT")
        join.set("kind", None)
        join.set("on", exp.true())
        if rest:
            select.where(exp.and_(*[r.copy() for r in rest]), copy=False)
        made.append(lateral)
    return made


def _inside(node: exp.Expression, root: exp.Expression) -> bool:
    while node is not None:
        if node is root:
            return True
        node = node.parent
    return False
