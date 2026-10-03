"""Bag identities for ``ORDER BY``, ``LIMIT`` and ``OFFSET``.

The prover compares result bags, so an ``ORDER BY`` matters only where a
``LIMIT`` or ``OFFSET`` cuts the sorted rows. A cut over an ordering with ties
can keep either of two tied rows; these rules only move or merge cuts when the
rows that come out are the same for every way of breaking ties.

* An ``ORDER BY`` without ``LIMIT``/``OFFSET`` that nothing order-sensitive reads
  is dropped (``_drop_unread_order``), and a key repeated in one ``ORDER BY``
  is dropped (``_dedupe_order_keys``).
* ``SELECT p FROM (SELECT q FROM t ORDER BY o LIMIT n) AS d`` is
  ``SELECT p∘q FROM t ORDER BY o LIMIT n`` at any depth (``_lift_cut``).
* An outer ``ORDER BY o LIMIT n OFFSET m`` over projections of an inner
  ``ORDER BY o .. LIMIT k OFFSET j`` is one cut, ``LIMIT min(n, k - m) OFFSET j + m``,
  and over ``UNION ALL`` an inner branch cut ``ORDER BY o LIMIT k`` with
  ``k >= n + m`` changes nothing, nor does such a cut on the whole ``UNION ALL`` (``_merge_top_k``). Both need every output column
  of the outer query to be a function of its ``ORDER BY`` keys: then the rows
  kept are fixed by the sorted key values alone, whichever tied rows each cut
  picks.
"""

from __future__ import annotations

import re

from sqlglot import exp

_VOLATILE = (exp.Subquery, exp.AggFunc, exp.Window, exp.Rand, exp.Anonymous, exp.Star)


def limit_rule(select: exp.Select, types: dict[str, dict[str, str]] | None = None, dialect: str = "bigquery") -> exp.Expression | None:
    """Apply the first identity that changes ``select``; ``None`` when none applies.

    ``types`` (table -> column -> declared type, lower case) lets an ORDER BY key drop a cast that
    keeps the order (``_order_by_uncast``)."""

    for rule in (_drop_unread_order, _dedupe_order_keys, _merge_top_k, _lift_cut):
        rewritten = rule(select)
        if rewritten is not None:
            return rewritten
    if types and dialect != "bigquery":
        return _order_by_uncast(select, types)
    return None


# --- small readers -----------------------------------------------------------------------


def _literal(node) -> int | None:
    if not isinstance(node, exp.Literal) or node.is_string:
        return None
    try:
        value = int(node.this)
    except ValueError:
        return None
    return value if value >= 0 else None


def _cut(query: exp.Expression):
    """``(limit, offset)`` of a literal cut (``limit`` ``None`` when unbounded), ``False`` for an unusable one,
    ``None`` without one."""

    limit, offset = query.args.get("limit"), query.args.get("offset")
    if limit is None and offset is None:
        return None
    count = None
    if limit is not None:
        options = limit.args.get("limit_options")
        if options is not None and (options.args.get("percent") or options.args.get("with_ties")):
            return False
        if isinstance(limit, exp.Limit) and not any(v for k, v in limit.args.items() if k not in ("expression", "limit_options")):
            count = _literal(limit.expression)
        elif isinstance(limit, exp.Fetch) and not any(v for k, v in limit.args.items() if k not in ("count", "direction", "limit_options")):
            count = _literal(limit.args.get("count"))
        if count is None:
            return False
    skip = 0
    if offset is not None:
        if not isinstance(offset, exp.Offset) or any(v for k, v in offset.args.items() if k != "expression"):
            return False
        skip = _literal(offset.expression)
        if skip is None:
            return False
    return count, skip


def _set_cut(query: exp.Expression, count: int | None, skip: int) -> None:
    query.set("limit", exp.Limit(expression=exp.Literal.number(count)) if count is not None else None)
    query.set("offset", exp.Offset(expression=exp.Literal.number(skip)) if skip else None)


def _source(select: exp.Select):
    """The single FROM item of ``select`` when it has no joins, else ``None``."""

    from_ = select.args.get("from_") or select.args.get("from")
    if from_ is None or select.args.get("joins") or select.args.get("laterals") or select.args.get("pivots"):
        return None
    return from_.this


def _body(node: exp.Expression) -> exp.Expression | None:
    """The query inside nested parentheses; ``None`` when a parenthesis carries its own clauses."""

    while isinstance(node, exp.Subquery):
        if any(node.args.get(k) for k in ("limit", "offset", "order")):
            return None
        node = node.this
    return node


def _first_select(query: exp.Expression) -> exp.Select | None:
    while isinstance(query, (exp.SetOperation, exp.Subquery)):
        query = query.this
    return query if isinstance(query, exp.Select) else None


def _outputs(query: exp.Expression):
    """Output names and expressions of a select, or names only (``None`` expressions) of a set operation."""

    first = _first_select(query)
    if first is None or any(isinstance(e, exp.Star) or (isinstance(e, exp.Column) and isinstance(e.this, exp.Star)) for e in first.expressions):
        return None
    # An unaliased expression has an engine-made name nothing can refer to: give it a name no column has.
    names = [e.alias_or_name.lower() if isinstance(e, (exp.Alias, exp.Column)) else f"\0{i}" for i, e in enumerate(first.expressions)]
    if isinstance(query, exp.Select):
        return names, [e.this if isinstance(e, exp.Alias) else e for e in query.expressions]
    return names, None


def _plain(select: exp.Select) -> bool:
    """A select that keeps its rows one for one: no filter, grouping, DISTINCT or window."""

    if any(select.args.get(k) for k in ("where", "group", "having", "distinct", "qualify", "windows", "with_", "with", "connect", "prewhere")):
        return False
    return not any(n.find_ancestor(exp.Select) is select for n in select.find_all(exp.AggFunc, exp.Window))


def _position_form(expr: exp.Expression, names: list[str] | None, alias: str | None) -> exp.Expression | None:
    """``expr`` over the columns of a relation with output ``names``, columns written ``__p<i>``; over a
    base table (``names`` ``None``) columns are written ``__n_<name>``."""

    copy = expr.copy()
    for column in list(copy.find_all(exp.Column)):
        if isinstance(column.this, exp.Star):
            return None
        if column.table and (alias is None or column.table.lower() != alias):
            return None
        if column.args.get("db") or column.args.get("catalog"):
            return None
        if names is None:
            replacement = exp.column(f"__n_{column.name.lower()}")
        else:
            matches = [i for i, n in enumerate(names) if n == column.name.lower()]
            if len(matches) != 1:
                return None
            replacement = exp.column(f"__p{matches[0]}")
        if column is copy:
            copy = replacement
        else:
            column.replace(replacement)
    return copy


def _substitute(expr: exp.Expression, values: list[exp.Expression]) -> exp.Expression | None:
    """Replace ``__p<i>`` in ``expr`` with ``values[i]``."""

    copy = expr.copy()
    for column in list(copy.find_all(exp.Column)):
        name = column.name
        if not name.startswith("__p") or column.table:
            return None
        value = values[int(name[3:])].copy()
        if column is copy:
            copy = value
        else:
            column.replace(value)
    return copy


def _reader(select: exp.Select):
    """``(names, alias)`` of the relation ``select`` reads: a derived table's output names, or ``None``
    names for a base table; ``None`` when it reads anything else."""

    source = _source(select)
    if isinstance(source, exp.Table):
        if source.args.get("joins") or isinstance(source.this, (exp.Anonymous, exp.Func)):
            return None
        return None, source.alias_or_name.lower()
    if not isinstance(source, exp.Subquery):
        return None
    inner = _body(source)
    read = _outputs(inner) if inner is not None else None
    if read is None:
        return None
    return read[0], (source.alias or "").lower() or None


def _lower(keys, select: exp.Select):
    """Keys over ``select``'s output positions rewritten over the relation it reads."""

    outputs, reader = _outputs(select), _reader(select)
    if outputs is None or reader is None:
        return None
    lowered = []
    for key, desc, nulls_first in keys:
        value = _substitute(key, outputs[1])
        form = _position_form(value, *reader) if value is not None and _deterministic(value) else None
        if form is None:
            return None
        lowered.append((form, desc, nulls_first))
    return lowered


def _deterministic(expr: exp.Expression) -> bool:
    return not any(isinstance(n, _VOLATILE) for n in expr.walk())


def _ordering(query: exp.Expression):
    """The ``ORDER BY`` of ``query`` as ``(key in position form, descending, nulls first)`` over the
    relation the keys read: the FROM source of a select, the output of a set operation."""

    order = query.args.get("order")
    if order is None:
        return None
    if isinstance(query, exp.Select):
        reader = _reader(query)
        if reader is None:
            return None
        names, alias = reader
        outputs = _outputs(query)
        if outputs is None:
            return None
        out_names, out_exprs = outputs
    else:
        read = _outputs(query)
        if read is None:
            return None
        names, alias, out_names, out_exprs = read[0], None, None, None
    keys = []
    for item in order.expressions:
        if not isinstance(item, exp.Ordered) or item.args.get("with_fill"):
            return None
        key = item.this
        if out_exprs is not None:
            if _literal(key) is not None and not isinstance(key, exp.Column):
                position = _literal(key)
                if not 1 <= position <= len(out_exprs):
                    return None
                key = out_exprs[position - 1]
            elif isinstance(key, exp.Column) and not key.table:
                named = [i for i, n in enumerate(out_names) if n == key.name.lower()]
                if named:
                    picked = {out_exprs[i].sql() for i in named}
                    if len(picked) != 1:
                        return None
                    value = out_exprs[named[0]]
                    # A name that is both an output alias and a different input column is read differently
                    # by different dialects.
                    if not (isinstance(value, exp.Column) and value.name.lower() == key.name.lower()) and (names is None or key.name.lower() in names):
                        return None
                    key = value
        elif _literal(key) is not None and not isinstance(key, exp.Column):
            position = _literal(key)
            if names is None or not 1 <= position <= len(names):
                return None
            key = exp.column(names[position - 1])
        elif any(c.table for c in key.find_all(exp.Column)):
            key = key.copy()
            for column in [c for c in key.find_all(exp.Column) if c.table]:
                position = _set_operation_column(query, column, names)
                if position is None:
                    return None
                if column is key:
                    key = exp.column(names[position])
                else:
                    column.replace(exp.column(names[position]))
        if not _deterministic(key):
            return None
        form = _position_form(key, names, alias)
        if form is None:
            return None
        desc = bool(item.args.get("desc"))
        nulls_first = item.args.get("nulls_first")
        keys.append((form, desc, (not desc) if nulls_first is None else bool(nulls_first)))
    return keys


def _set_operation_column(query: exp.Expression, key: exp.Column, names: list[str]) -> int | None:
    """The output position a qualified column ``t.c`` in a set operation's ORDER BY reads, or ``None``.

    Engines differ here: some reject it, DuckDB matches it against the branches' select lists. Only
    a reading every engine that accepts it agrees on is taken: ``c`` names exactly one output, and
    every branch item that is ``t.c`` sits at that same position."""

    if key.args.get("db") or key.args.get("catalog") or isinstance(key.this, exp.Star):
        return None
    named = [i for i, n in enumerate(names) if n == key.name.lower()]
    if len(named) != 1:
        return None
    branches, found = [query.this, query.expression], set()
    while branches:
        branch = branches.pop()
        if isinstance(branch, exp.SetOperation):
            branches += [branch.this, branch.expression]
            continue
        if isinstance(branch, exp.Subquery):
            branches.append(branch.this)
            continue
        if not isinstance(branch, exp.Select):
            return None
        for i, item in enumerate(branch.expressions):
            value = item.this if isinstance(item, exp.Alias) else item
            if isinstance(value, exp.Column) and value.name.lower() == key.name.lower() and value.table.lower() == key.table.lower():
                found.add(i)
    return named[0] if found == {named[0]} else None


# --- dropping an unread ORDER BY -------------------------------------------------------------


def _order_unread(query: exp.Expression) -> bool:
    """Whether the order of ``query``'s rows can never show: it feeds only bag operators up to the root."""

    node = query
    while node.parent is not None:
        parent = node.parent
        if isinstance(parent, exp.Subquery):
            if any(parent.args.get(k) for k in ("limit", "offset")):
                return False
        elif isinstance(parent, (exp.From, exp.Join)):
            pass
        elif isinstance(parent, exp.SetOperation):
            if node.arg_key not in ("this", "expression"):
                return False
            if (parent.args.get("limit") or parent.args.get("offset")) and not parent.args.get("order"):
                return False
        elif isinstance(parent, exp.Select):
            if node.arg_key not in ("from", "from_", "joins"):
                return False
            if (parent.args.get("limit") or parent.args.get("offset")) and not parent.args.get("order"):
                return False
            if any(w.find_ancestor(exp.Select) is parent for w in parent.find_all(exp.Window)):
                return False
        elif isinstance(parent, (exp.In, exp.Exists)):
            # A membership or existence test reads a set: neither order nor ties show.
            return node.arg_key in ("query", "this")
        else:
            return False
        node = parent
    return True


def _drop_unread_order(select: exp.Select) -> exp.Expression | None:
    changed = False
    targets = [select]
    source = _source(select)
    if isinstance(source, exp.Subquery) and isinstance(source.this, exp.SetOperation):
        targets.append(source.this)
    for query in targets:
        if query.args.get("order") is not None and query.args.get("limit") is None and query.args.get("offset") is None and _order_unread(query):
            query.set("order", None)
            changed = True
    return select if changed else None


def _dedupe_order_keys(select: exp.Select) -> exp.Expression | None:
    """``ORDER BY a, b, a`` is ``ORDER BY a, b``: rows tied on ``a`` stay tied on ``a``."""

    order = select.args.get("order")
    if order is None:
        return None
    seen: set[str] = set()
    kept = []
    for item in order.expressions:
        key = item.this.sql() if isinstance(item, exp.Ordered) else None
        if key is not None and key in seen and _deterministic(item.this):
            continue
        if key is not None:
            seen.add(key)
        kept.append(item)
    if len(kept) == len(order.expressions):
        return None
    order.set("expressions", kept)
    return select


# A 32-bit integer column cast to one of these keeps distinct values distinct and in order.
_SMALL_INT = re.compile(r"^\s*(tinyint|smallint|mediumint|int|integer|int2|int4)\b", re.I)
_WIDE = (exp.DataType.Type.DOUBLE, exp.DataType.Type.BIGINT)


def _order_by_uncast(select: exp.Select, types: dict[str, dict[str, str]]) -> exp.Expression | None:
    """``ORDER BY CAST(i AS DOUBLE)`` is ``ORDER BY i`` for a 32-bit integer column ``i``: the cast is
    strictly increasing, so it neither reorders rows nor makes or breaks ties (NULL stays NULL).

    Only for dialects whose ``INT`` is 32 bits (BigQuery's is 64, and a DOUBLE cannot hold every
    64-bit integer, so it could tie two values)."""

    from .algebraic_equivalence import _origin_type

    order = select.args.get("order")
    if order is None:
        return None
    outputs: dict[str, list[exp.Expression]] = {}
    for e in select.expressions:
        if isinstance(e, (exp.Alias, exp.Column)):
            outputs.setdefault(e.alias_or_name.lower(), []).append(e.this if isinstance(e, exp.Alias) else e)
    changed = False
    for item in order.expressions:
        cast = item.this if isinstance(item, exp.Ordered) else None
        if type(cast) is not exp.Cast or not isinstance(cast.this, exp.Column) or not isinstance(cast.args.get("to"), exp.DataType):
            continue
        if cast.args["to"].this not in _WIDE or cast.args["to"].expressions:
            continue
        column = cast.this
        named = outputs.get(column.name.lower(), [])
        if not column.table and any(not (isinstance(v, exp.Column) and v.name.lower() == column.name.lower()) for v in named):
            continue  # the name may mean an output alias
        declared = _origin_type(select, column, types)
        if declared and _SMALL_INT.match(declared):
            item.set("this", column.copy())
            changed = True
    return select if changed else None


# --- merging cuts -----------------------------------------------------------------------------


def _total(select: exp.Select, keys) -> bool:
    """Every output column of ``select`` is a function of its ORDER BY keys (``keys`` in position form)."""

    reader, outputs = _reader(select), _outputs(select)
    if reader is None or outputs is None:
        return False
    texts = {k.sql() for k, _, _ in keys}
    for expr in outputs[1]:
        if not _deterministic(expr):
            return False
        form = _position_form(expr, *reader)
        if form is None:
            return False
        if form.sql() in texts:
            continue
        if not all(c.sql() in texts for c in form.find_all(exp.Column)):
            return False
    return True


def _cuts_below(relation: exp.Expression, keys, through_union: bool):
    """Inner cuts reached from ``relation`` through pass-through selects and ``UNION ALL``, each with
    the outer keys rewritten over its own relation and whether a ``UNION ALL`` was crossed."""

    query = _body(relation)
    if query is None:
        return
    if query.args.get("order") is not None and (query.args.get("limit") is not None or query.args.get("offset") is not None):
        yield query, keys, through_union
        return
    if any(query.args.get(k) for k in ("order", "limit", "offset")):
        return
    if isinstance(query, exp.Select):
        source = _source(query)
        if not _plain(query) or not isinstance(source, exp.Subquery):
            return
        lowered = _lower(keys, query)
        if lowered is not None:
            yield from _cuts_below(source, lowered, through_union)
    elif type(query) is exp.Union and not query.args.get("distinct"):
        if any(query.args.get(k) for k in ("by_name", "side", "kind", "on")):
            return
        for branch in (query.this, query.expression):
            yield from _cuts_below(branch, keys, True)


def _key_text(keys):
    return [(k.sql(), d, n) for k, d, n in keys]


def _merge_top_k(select: exp.Select) -> exp.Expression | None:
    order = select.args.get("order")
    cut = _cut(select)
    if order is None or not cut or not _plain(select):
        return None
    count, skip = cut
    source = _source(select)
    if not isinstance(source, exp.Subquery):
        return None
    keys = _ordering(select)
    if not keys or not _total(select, keys):
        return None
    changed = False
    for inner, lowered, through_union in list(_cuts_below(source, keys, False)):
        inner_cut = _cut(inner)
        if not inner_cut:
            continue
        inner_count, inner_skip = inner_cut
        if isinstance(inner, exp.Select):
            # The lowered keys read the inner select's output; its own ORDER BY reads its source.
            lowered = _lower(lowered, inner)
            if lowered is None:
                continue
        own = _ordering(inner)
        if own is None or _key_text(own)[: len(lowered)] != _key_text(lowered):
            continue
        # A branch (or a whole set operation) that keeps at least the n + m first rows of its own order
        # loses none of the rows the outer cut keeps.
        keeps_enough = not inner_skip and (inner_count is None or (count is not None and inner_count >= count + skip))
        if through_union or (keeps_enough and isinstance(inner, exp.SetOperation)):
            if not keeps_enough:
                continue
            inner.set("order", None)
            _set_cut(inner, None, 0)
            changed = True
            continue
        if not _order_unread(select):
            continue
        if inner_count is None:
            merged = count
        elif count is None:
            merged = max(inner_count - skip, 0)
        else:
            merged = min(count, max(inner_count - skip, 0))
        _set_cut(inner, merged, inner_skip + skip)
        select.set("order", None)
        _set_cut(select, None, 0)
        return select
    return select if changed else None


# --- lifting a cut out of a projection --------------------------------------------------------


def _lift_cut(select: exp.Select) -> exp.Expression | None:
    """``SELECT f(c) FROM (SELECT g AS c FROM t ORDER BY o LIMIT n) AS d`` is ``SELECT f(g) FROM t ORDER BY o LIMIT n``.

    The outer select computes a value per row of the cut, so the cut can be taken first and the
    values after, in one select. ``_lift_limit_derived`` does this at the root for plain columns
    and a LIMIT; this also works below the root, for expressions and for OFFSET without LIMIT.
    """

    source = _source(select)
    if not isinstance(source, exp.Subquery) or not _plain(select) or any(select.args.get(k) for k in ("order", "limit", "offset")):
        return None
    inner = source.this
    if not isinstance(inner, exp.Select) or inner.args.get("order") is None or not _cut(inner):
        return None
    if any(inner.args.get(k) for k in ("distinct", "qualify", "with_", "with", "windows")):
        return None
    if any(w.find_ancestor(exp.Select) is inner for w in inner.find_all(exp.Window)):
        return None
    outputs = _outputs(inner)
    if outputs is None or len(set(outputs[0])) != len(outputs[0]) or "" in outputs[0]:
        return None
    names, values = outputs
    alias = (source.alias or "").lower() or None
    items = []
    for item in select.expressions:
        value = item.this if isinstance(item, exp.Alias) else item
        if isinstance(value, exp.Star):
            return None
        form = _position_form(value, names, alias)
        lifted = _substitute(form, values) if form is not None else None
        if lifted is None:
            return None
        name = item.alias_or_name
        items.append(exp.alias_(lifted, name) if name and not (isinstance(lifted, exp.Column) and lifted.name == name) else lifted)
    # The inner ORDER BY may name the inner select's outputs; spell those out before the list changes.
    keys = []
    inner_names = {n: v for n, v in zip(names, values)}
    for ordered in inner.args["order"].expressions:
        if not isinstance(ordered, exp.Ordered):
            return None
        key = ordered.this
        if _literal(key) is not None and not isinstance(key, exp.Column):
            position = _literal(key)
            if not 1 <= position <= len(values):
                return None
            key = values[position - 1]
        elif isinstance(key, exp.Column) and not key.table and key.name.lower() in inner_names:
            key = inner_names[key.name.lower()]
        else:
            for column in key.find_all(exp.Column):
                if column.table or column.name.lower() not in inner_names:
                    continue
                value = inner_names[column.name.lower()]
                if not (isinstance(value, exp.Column) and value.name.lower() == column.name.lower()):
                    return None  # an output alias inside an ORDER BY expression
        keys.append(ordered.copy())
        keys[-1].set("this", key.copy())
    new_names = {i.alias_or_name.lower(): (i.this if isinstance(i, exp.Alias) else i) for i in items}
    for ordered in keys:
        for column in ordered.this.find_all(exp.Column):
            value = new_names.get(column.name.lower())
            if not column.table and value is not None and value.sql() != column.sql():
                return None
    result = inner.copy()
    result.set("expressions", items)
    result.set("order", exp.Order(expressions=keys))
    return result
