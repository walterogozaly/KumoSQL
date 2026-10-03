"""Fold a regrouping that counts tuples of grouping keys back into ``COUNT(DISTINCT a, b, ..)``.

``SELECT k, COUNT(a, b), SUM(p) FROM (SELECT k, a, b, SUM(x) AS p FROM t GROUP BY k, a, b) AS g GROUP BY k``
is ``SELECT k, COUNT(DISTINCT a, b), SUM(x) FROM t GROUP BY k`` (Calcite's plan for a multi-argument
``COUNT(DISTINCT ..)``). Within one outer group (one value of ``k``) the inner groups are the distinct
tuples of the extra keys ``(a, b)`` among the group's rows, NULLs included, one inner row each:

* ``COUNT(c1, .., cn)`` of key columns that include every extra key counts the inner rows whose
  ``c1 .. cn`` are all non-NULL. Those columns and ``k`` (fixed in the group) determine the inner
  group, so the inner rows match one to one the distinct ``(c1, .., cn)`` tuples of the group's rows
  with no NULL component, which ``COUNT(DISTINCT c1, .., cn)`` counts.
* ``MIN``/``MAX`` of a key column see the same set of values as over the rows (both ignore NULLs
  and duplicates).
* ``SUM`` of a partial ``SUM`` or ``COUNT``, and ``MIN``/``MAX`` of a partial of the same kind, is
  that aggregate over the rows: an outer group is never empty, a partial ``SUM`` is NULL only when
  all its rows are, and partial ``COUNT``s are never NULL.

Anything else (``COUNT(*)`` over the inner rows counts NULL tuples too, a HAVING, a filter or join in
the outer query, a COUNT that misses an extra key) leaves the query alone. The rule only fires
when it produces a multi-argument ``COUNT(DISTINCT ..)``; one extra key is ``_regroup_distinct``'s case.
"""

from __future__ import annotations

from sqlglot import exp

_BANNED = ("distinct", "order", "limit", "offset", "qualify", "windows", "with", "with_", "having")


def _plain(select: exp.Select) -> bool:
    if any(select.args.get(k) for k in _BANNED) or any(select.find_all(exp.Window)):
        return False
    group = select.args.get("group")
    if not group or not group.expressions or any(group.args.get(k) for k in ("grouping_sets", "rollup", "cube", "totals")):
        return False
    return all(isinstance(k, exp.Column) for k in group.expressions)


def _partial(node: exp.Expression) -> bool:
    return (
        isinstance(node, (exp.Sum, exp.Count, exp.Min, exp.Max))
        and not node.args.get("distinct")
        and not isinstance(node.this, exp.Distinct)
        and not node.args.get("expressions")
    )


def regroup_tuple_count(select: exp.Select) -> exp.Expression | None:
    if not _plain(select) or select.args.get("where") or select.args.get("joins") or select.args.get("laterals"):
        return None
    from_ = select.args.get("from_") or select.args.get("from")
    source = from_.this if from_ else None
    if not isinstance(source, exp.Subquery) or not isinstance(source.this, exp.Select):
        return None
    inner = source.this
    if not _plain(inner):
        return None
    alias = (source.alias or "").lower()
    inner_keys = {k.sql() for k in inner.args["group"].expressions}
    keys: dict[str, exp.Column] = {}
    partials: dict[str, exp.Expression] = {}
    names: set[str] = set()
    for item in inner.expressions:
        value = item.this if isinstance(item, exp.Alias) else item
        name = (item.alias_or_name or "").lower()
        if not name or name in names:
            return None
        names.add(name)
        if isinstance(value, exp.Column) and value.sql() in inner_keys:
            keys[name] = value
        elif _partial(value):
            partials[name] = value
        else:
            return None

    def read(node: exp.Expression) -> str | None:
        """The inner output an outer column reads, by name."""

        if not isinstance(node, exp.Column) or (node.table and node.table.lower() != alias):
            return None
        return node.name.lower()

    outer_keys = []
    for k in select.args["group"].expressions:
        name = read(k)
        if name not in keys:
            return None
        outer_keys.append(keys[name])
    extra = inner_keys - {k.sql() for k in outer_keys}
    if not extra:
        return None

    items: list[exp.Expression] = []
    tuples = 0
    for item in select.expressions:
        value = item.this if isinstance(item, exp.Alias) else item
        name = item.alias if isinstance(item, exp.Alias) else ""
        if isinstance(value, (exp.Literal, exp.Null, exp.Boolean)):
            replacement = value.copy()
        elif isinstance(value, exp.Column):
            column = read(value)
            if column not in keys or keys[column].sql() in extra:
                return None
            replacement = keys[column].copy()
            name = name or value.name
        elif isinstance(value, exp.Count) and not value.args.get("distinct") and not isinstance(value.this, exp.Distinct):
            arguments = [value.this, *(value.args.get("expressions") or [])]
            columns = [read(a) for a in arguments]
            if any(c not in keys for c in columns):
                return None
            inner_columns = [keys[c] for c in columns]
            if not extra <= {c.sql() for c in inner_columns} or len(arguments) < 2:
                return None
            replacement = exp.Count(this=exp.Distinct(expressions=[c.copy() for c in inner_columns]))
            tuples += 1
        elif isinstance(value, (exp.Sum, exp.Min, exp.Max)) and _partial(value):
            column = read(value.this)
            if column in keys and isinstance(value, (exp.Min, exp.Max)):
                replacement = type(value)(this=keys[column].copy())
            elif column in partials and (
                type(partials[column]) is type(value) or (isinstance(value, exp.Sum) and isinstance(partials[column], exp.Count))
            ):
                replacement = partials[column].copy()
            else:
                return None
        else:
            return None
        items.append(exp.alias_(replacement, name) if name else replacement)
    if not tuples:
        return None
    result = inner.copy()
    result.set("expressions", items)
    result.set("group", exp.Group(expressions=[k.copy() for k in outer_keys]))
    return result
