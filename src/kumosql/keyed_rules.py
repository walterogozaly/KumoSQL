"""Rewrites that rest on declared keys and on aggregates that always return a row.

* A ``GROUP BY`` over one table that lists a NOT NULL key puts every row in its
  own group, so each aggregate reads that row's value: ``SUM(x)``, ``MIN``,
  ``MAX``, ``AVG``, ``BIT_AND``/``BIT_OR`` (with or without ``DISTINCT``) are
  ``x``, ``COUNT(*)`` is 1, ``COUNT(x)`` is 1 unless ``x`` is NULL, and
  ``GROUPING(c)`` is 0. A key column the WHERE clause equates to a
  constant counts as grouped, and a ``HAVING`` becomes part of the WHERE. The
  grouping is dropped, and so is a ``DISTINCT``
  that outputs such a key.
* ``EXISTS`` over a select whose outputs are all aggregates and that has no
  ``GROUP BY``, ``HAVING`` or ``LIMIT`` always finds its one row, so it is TRUE.
"""

from __future__ import annotations

from sqlglot import exp

_VALUE_AGGREGATES = (exp.Sum, exp.Min, exp.Max, exp.Avg)
_VALUE_CLASSES = ("BitwiseAndAgg", "BitwiseOrAgg", "BitwiseXorAgg", "AnyValue")
_VALUE_NAMES = {"BIT_AND", "BIT_OR", "BIT_XOR", "ANY_VALUE", "SINGLE_VALUE"}


def _single_table(select: exp.Select) -> exp.Table | None:
    source = select.args.get("from_") or select.args.get("from")
    if source is None or select.args.get("joins") or select.args.get("laterals"):
        return None
    table = source.this
    return table if isinstance(table, exp.Table) else None


def _aggregate_kind(node: exp.Expression) -> str | None:
    if isinstance(node, exp.Count):
        return "count"
    if isinstance(node, _VALUE_AGGREGATES):
        return "value"
    if isinstance(node, exp.Anonymous) and node.name.upper() in _VALUE_NAMES and len(node.expressions) == 1:
        return "value"
    if isinstance(node, tuple(getattr(exp, n) for n in _VALUE_CLASSES if hasattr(exp, n))):
        return "value"
    if isinstance(node, tuple(getattr(exp, n) for n in ("Grouping",) if hasattr(exp, n))) or (
        isinstance(node, exp.Anonymous) and node.name.upper() == "GROUPING"
    ):
        return "grouping"
    if isinstance(node, exp.AggFunc):
        return "other"
    return None


def _argument(node: exp.Expression) -> exp.Expression | None:
    arg = node.expressions[0] if isinstance(node, exp.Anonymous) else node.this
    if isinstance(arg, exp.Distinct):
        if len(arg.expressions) != 1:
            return None
        arg = arg.expressions[0]
    return arg


def remove_keyed_grouping(
    select: exp.Select, keys: dict[str, list[tuple[str, ...]]] | None, not_null: dict[str, frozenset[str]] | None
) -> exp.Expression | None:
    group = select.args.get("group")
    if not group or not keys or any(select.args.get(k) for k in ("qualify", "distinct")):
        return None
    if any(group.args.get(k) for k in ("grouping_sets", "cube", "rollup", "totals")):
        return None
    table = _single_table(select)
    if table is None:
        return None
    name = table.name.lower()
    alias = table.alias_or_name.lower()
    grouped = set()
    for key in group.expressions:
        if not isinstance(key, exp.Column) or (key.table and key.table.lower() not in (alias, name)):
            continue
        grouped.add(key.name.lower())
    grouped |= _fixed_columns(select, name, alias)
    declared = {c.lower() for c in (not_null or {}).get(name, frozenset())}
    if not any(set(k) <= grouped and set(k) <= declared for k in ((tuple(c.lower() for c in key) for key in keys.get(name, [])))):
        return None
    # A window over the grouped rows would see different rows once the grouping goes.
    if any(w.find_ancestor(exp.Select) is select for w in select.find_all(exp.Window)):
        return None
    copy = select.copy()
    for node in list(copy.find_all(exp.AggFunc, exp.Anonymous, *(getattr(exp, n) for n in ("Grouping",) if hasattr(exp, n)))):
        if node.find_ancestor(exp.Select) is not copy or node.find_ancestor(exp.AggFunc, exp.Window) is not None:
            continue
        kind = _aggregate_kind(node)
        if kind is None:
            continue
        if kind == "other":
            return None
        if kind == "grouping":
            node.replace(exp.Literal.number(0))
            continue
        if kind == "count" and (node.this is None or isinstance(node.this, exp.Star)):
            node.replace(exp.Literal.number(1))
            continue
        arg = _argument(node)
        if arg is None or arg.find(exp.AggFunc, exp.Select, exp.Window) is not None:
            return None
        if kind == "count":
            node.replace(
                exp.Case(
                    ifs=[exp.If(this=exp.Is(this=arg.copy(), expression=exp.Null()), true=exp.Literal.number(0))],
                    default=exp.Literal.number(1),
                )
            )
        else:
            node.replace(arg.copy())
    copy.set("group", None)
    having = copy.args.get("having")
    if having is not None:
        copy.set("having", None)
        copy.where(having.this.copy(), copy=False)
    return copy


def _fixed_columns(select: exp.Select, name: str, alias: str) -> set[str]:
    """Columns the WHERE clause equates to a constant (``WHERE k = 10``): one value in every row."""

    where = select.args.get("where")
    fixed = set()
    if where is None:
        return fixed
    parts, todo = [], [where.this]
    while todo:
        node = todo.pop()
        if isinstance(node, exp.And):
            todo += [node.left, node.right]
        elif isinstance(node, exp.Paren):
            todo.append(node.this)
        else:
            parts.append(node)
    for part in parts:
        if not isinstance(part, exp.EQ):
            continue
        for column, other in ((part.left, part.right), (part.right, part.left)):
            if isinstance(column, exp.Column) and isinstance(other, exp.Literal) and (not column.table or column.table.lower() in (alias, name)):
                fixed.add(column.name.lower())
    return fixed


def drop_keyed_distinct(
    select: exp.Select, keys: dict[str, list[tuple[str, ...]]] | None, not_null: dict[str, frozenset[str]] | None
) -> exp.Expression | None:
    """``SELECT DISTINCT`` over one table that outputs a NOT NULL key never sees two equal rows."""

    if not select.args.get("distinct") or select.args["distinct"].args.get("on") or not keys:
        return None
    if any(select.args.get(k) for k in ("group", "having", "qualify")):
        return None
    table = _single_table(select)
    if table is None:
        return None
    name, alias = table.name.lower(), table.alias_or_name.lower()
    outputs = set()
    for projection in select.expressions:
        value = projection.this if isinstance(projection, exp.Alias) else projection
        if isinstance(value, exp.Column) and (not value.table or value.table.lower() in (alias, name)):
            outputs.add(value.name.lower())
    outputs |= _fixed_columns(select, name, alias)
    declared = {c.lower() for c in (not_null or {}).get(name, frozenset())}
    if not any(set(k) <= outputs and set(k) <= declared for k in (tuple(c.lower() for c in key) for key in keys.get(name, []))):
        return None
    copy = select.copy()
    copy.set("distinct", None)
    return copy


def _always_one_row(select: exp.Expression) -> bool:
    if not isinstance(select, exp.Select) or not select.expressions:
        return False
    if any(select.args.get(k) for k in ("group", "having", "qualify", "limit", "offset", "distinct")):
        return False
    for projection in select.expressions:
        value = projection.this if isinstance(projection, exp.Alias) else projection
        if not isinstance(value, exp.AggFunc) or value.find(exp.Window) is not None:
            return False
    return True


def exists_over_aggregate(tree: exp.Expression) -> exp.Expression:
    def step(node: exp.Expression) -> exp.Expression:
        if isinstance(node, exp.Exists) and _always_one_row(node.this):
            return exp.true()
        return node

    return tree.transform(step)
