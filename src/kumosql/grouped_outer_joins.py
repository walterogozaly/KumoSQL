"""Outer joins of duplicate-free relations, and outer joins an anti-join test rules out.

Two rewrites, both on a SELECT with exactly one LEFT or RIGHT join:

**Grouping a join that is already duplicate-free.** In
``SELECT a.x, b.y FROM (SELECT x FROM A GROUP BY x) a LEFT JOIN (SELECT y FROM B GROUP BY y) b ON a.x = b.y``
the rows of ``a`` are distinct and so are the rows of ``b``. The join holds each matching pair ``(a, b)``
once and each unmatched row of ``a`` once with NULLs for ``b`` (never both for one row of ``a``), so its
rows are distinct; the select lists every column of both inputs, so its output rows are distinct too
and a ``GROUP BY`` over all of the select's columns is a no-op. Stating it lets the query be read like
the grouping it came from, ``SELECT a.x, b.y FROM A a LEFT JOIN B b ON a.x = b.y GROUP BY a.x, b.y``, once
the grouping inside each input is seen to be redundant. Both inputs must be derived tables whose every
output is a group key (or a DISTINCT output) and whose every group key is an output: a hidden group key
would let rows repeat. Nothing is assumed about the ON condition.

**A left join that an anti-join test rules out.** In
``SELECT .. FROM p LEFT JOIN t AS o ON c WHERE NOT EXISTS (SELECT .. FROM t AS s WHERE c')`` with ``c'``
the ON condition ``c`` spelled over ``s`` instead of ``o``, a row of ``p`` passes the test exactly when it
has no match in the join, and then the join gives it one null-extended row. A row of ``p`` with matches
fails the test on every joined row (the test reads only ``p``), so those rows all go. The join is
therefore ``p`` with NULL for every column of ``o``. The test must be a top-level conjunct of WHERE, the
correlated columns of ``c'`` must come from ``p``, and the two must read the same base table unfiltered.
"""

from __future__ import annotations

from sqlglot import exp

from .ast_utils import select_sources

_EXTRAS = ("order", "limit", "offset", "qualify", "windows", "with_", "with", "into", "locks", "sample", "connect", "prewhere")


def grouped_outer_join_rules(select: exp.Select) -> exp.Expression | None:
    return group_distinct_outer_join(select) or null_extend_anti_joined(select)


def _from(select: exp.Select) -> exp.From | None:
    return select.args.get("from_") or select.args.get("from")


def _one_outer_join(select: exp.Select) -> tuple[exp.Join, str] | None:
    joins = select.args.get("joins") or []
    if _from(select) is None or len(joins) != 1:
        return None
    join = joins[0]
    side = (join.args.get("side") or "").upper()
    kind = (join.args.get("kind") or "").upper()
    if side not in ("LEFT", "RIGHT") or kind not in ("", "OUTER") or join.args.get("method"):
        return None
    if join.args.get("using") or join.args.get("on") is None:
        return None
    return join, side


def _unparen(node: exp.Expression) -> exp.Expression:
    while isinstance(node, exp.Paren):
        node = node.this
    return node


def _conjuncts(node: exp.Expression) -> list[exp.Expression]:
    node = _unparen(node)
    if isinstance(node, exp.And):
        return _conjuncts(node.this) + _conjuncts(node.expression)
    return [node]


def _has_star(select: exp.Select) -> bool:
    return any(isinstance(star, exp.Star) and not isinstance(star.parent, exp.Count) for star in select.find_all(exp.Star))


# --- grouping a duplicate-free outer join ---------------------------------------------------------------


def _distinct_outputs(source: exp.Expression) -> list[str] | None:
    """The output names of a derived table whose rows are distinct over all of its outputs, else None."""

    if not isinstance(source, exp.Subquery) or not source.alias or not isinstance(source.this, exp.Select):
        return None
    inner = source.this
    if source.args.get("alias") is not None and source.args["alias"].args.get("columns"):
        return None
    if any(inner.args.get(k) for k in _EXTRAS) or _has_star(inner) or inner.args.get("having"):
        return None
    names = [e.alias_or_name.lower() for e in inner.expressions]
    if not names or "" in names or len(set(names)) != len(names):
        return None
    distinct = inner.args.get("distinct")
    group = inner.args.get("group")
    if distinct is not None:
        if distinct.args.get("on") is not None or group is not None:
            return None
        return names
    if group is None or any(group.args.get(k) for k in ("rollup", "cube", "grouping_sets", "totals")):
        return None
    keys = group.expressions
    if not keys or any(not isinstance(k, exp.Column) for k in keys):
        return None
    outputs = [e.this if isinstance(e, exp.Alias) else e for e in inner.expressions]
    if any(not isinstance(o, exp.Column) for o in outputs):
        return None
    key_text = {k.sql().lower() for k in keys}
    output_text = {o.sql().lower() for o in outputs}
    # every output is a group key and every group key is an output: no hidden key, so no repeated rows
    if key_text != output_text:
        return None
    return names


def group_distinct_outer_join(select: exp.Select) -> exp.Expression | None:
    found = _one_outer_join(select)
    if found is None or isinstance(select.parent, exp.Subquery):
        # a derived table is left alone: the select reading it decides on duplicates (and an outer join
        # under a grouping is read as such a derived table, which this rule would only group again)
        return None
    join, _ = found
    if any(select.args.get(k) for k in _EXTRAS) or select.args.get("distinct") or select.args.get("group") or select.args.get("having"):
        return None
    if _has_star(select) or any(select.find_all(exp.Window, exp.AggFunc)):
        return None
    sources = [_from(select).this, join.this]
    outputs = [_distinct_outputs(source) for source in sources]
    if any(names is None for names in outputs):
        return None
    aliases = [source.alias.lower() for source in sources]
    if aliases[0] == aliases[1]:
        return None
    columns = []
    for item in select.expressions:
        value = item.this if isinstance(item, exp.Alias) else item
        if not isinstance(value, exp.Column) or value.table.lower() not in aliases:
            return None
        columns.append(value)
    # every column of both inputs is an output, so distinct joined pairs give distinct output rows
    for alias, names in zip(aliases, outputs):
        if not set(names) <= {c.name.lower() for c in columns if c.table.lower() == alias}:
            return None
    copy = select.copy()
    copy.set("group", exp.Group(expressions=[c.copy() for c in columns]))
    return copy


# --- a left join ruled out by NOT EXISTS ----------------------------------------------------------------


def _table_key(table: exp.Table) -> tuple[str, ...]:
    return tuple(p.name.lower() for p in (table.args.get("catalog"), table.args.get("db"), table.this) if p is not None and p.name)


def _condition_text(node: exp.Expression, rename: dict[str, str]) -> str:
    copy = _unparen(node).copy()
    for column in copy.find_all(exp.Column):
        table = column.table.lower()
        column.set("table", exp.to_identifier(rename.get(table, table)))
        column.set("this", exp.to_identifier(column.name.lower()))
    return _symmetric_text(copy)


def _symmetric_text(node: exp.Expression) -> str:
    """Text of a plain condition with the operands of ``=``/``<>`` and the parts of AND in a fixed order."""

    node = _unparen(node)
    if isinstance(node, exp.And):
        return "AND(" + ", ".join(sorted(_symmetric_text(part) for part in _conjuncts(node))) + ")"
    if isinstance(node, (exp.EQ, exp.NEQ)):
        sides = sorted(_symmetric_text(side) for side in (node.this, node.expression))
        return f"{type(node).__name__}({sides[0]}, {sides[1]})"
    return node.sql()


_PLAIN = (exp.Column, exp.Identifier, exp.Literal, exp.Null, exp.Boolean, exp.Paren, exp.And, exp.Or, exp.Not, exp.Is,
          exp.EQ, exp.NEQ, exp.GT, exp.GTE, exp.LT, exp.LTE)


def _plain_condition(node: exp.Expression) -> bool:
    """Comparisons of columns and constants only: deterministic, no subquery."""

    return all(isinstance(n, _PLAIN) for n in node.walk())


def _anti_test(part: exp.Expression, table: exp.Table, alias: str, on: exp.Expression) -> bool:
    """``part`` is ``NOT EXISTS (SELECT .. FROM <table> AS s WHERE <on with s for alias>)``."""

    part = _unparen(part)
    if not isinstance(part, exp.Not) or not isinstance(_unparen(part.this), exp.Exists):
        return False
    sub = _unparen(part.this).this
    if not isinstance(sub, exp.Select) or sub.args.get("joins") or _from(sub) is None or sub.args.get("where") is None:
        return False
    if any(sub.args.get(k) for k in _EXTRAS + ("group", "having", "distinct")):
        return False
    source = _from(sub).this
    if not isinstance(source, exp.Table) or _table_key(source) != _table_key(table) or not source.alias_or_name:
        return False
    if source.args.get("alias") is not None and source.args["alias"].args.get("columns"):
        return False
    inner = source.alias_or_name.lower()
    condition = sub.args["where"].this
    if not _plain_condition(condition):
        return False
    tables = {c.table.lower() for c in condition.find_all(exp.Column)}
    if "" in tables:
        return False
    # with a different alias inside, a column of the joined ``alias`` would be a correlated read of the join
    if inner != alias and alias in tables:
        return False
    return _condition_text(condition, {inner: alias}) == _condition_text(on, {})


def null_extend_anti_joined(select: exp.Select) -> exp.Expression | None:
    found = _one_outer_join(select)
    where = select.args.get("where")
    if found is None or where is None or _has_star(select):
        return None
    join, side = found
    left, right = _from(select).this, join.this
    kept, dropped = (left, right) if side == "LEFT" else (right, left)
    if not isinstance(dropped, exp.Table) or not isinstance(kept, (exp.Table, exp.Subquery)) or not dropped.alias_or_name or not kept.alias_or_name:
        return None
    if dropped.args.get("alias") is not None and dropped.args["alias"].args.get("columns"):
        return None
    alias = dropped.alias_or_name.lower()
    if alias == kept.alias_or_name.lower():
        return None
    on = join.args["on"]
    if not _plain_condition(on) or any(not c.table for c in on.find_all(exp.Column)):
        return None
    parts = _conjuncts(where.this)
    if not any(_anti_test(part, dropped, alias, on) for part in parts):
        return None
    group = select.args.get("group")
    if group is not None and any(
        not isinstance(key, exp.Column) and any(c.table.lower() == alias for c in key.find_all(exp.Column)) for key in group.expressions
    ):
        return None
    copy = select.copy()
    copy.set("joins", None)
    copy.set("from_" if "from_" in copy.args else "from", exp.From(this=kept.copy()))
    for column in list(copy.find_all(exp.Column)):
        scope = column.find_ancestor(exp.Select)
        if not column.table and scope is copy:
            return None  # an unqualified column could be a column of the dropped table
        if column.table.lower() != alias:
            continue
        if scope is not copy:
            # a nested select that names its own source ``alias`` reads that source; any other read of the
            # dropped table from inside a subquery is left alone
            shadowed = False
            while scope is not None and scope is not copy:
                if any((s.alias_or_name or "").lower() == alias for s in select_sources(scope)):
                    shadowed = True
                    break
                scope = scope.parent.find_ancestor(exp.Select) if scope.parent is not None else None
            if not shadowed:
                return None
            continue
        if column.parent is copy:
            column.replace(exp.alias_(exp.Null(), column.name))
        else:
            column.replace(exp.Null())
    group = copy.args.get("group")
    if group is not None:
        keys = [k for k in group.expressions if not isinstance(k, exp.Null)]
        if not keys or any(group.args.get(k) for k in ("rollup", "cube", "grouping_sets", "totals")):
            return None
        group.set("expressions", keys)
    return copy
