"""DISTINCT and regrouping rules for the algebraic normalizer.

Each rule takes one ``SELECT`` and returns a rewritten node, or None when it does not apply.
``distinct_rules`` tries them in order and is the single entry in
``algebraic_equivalence.normalize``'s rule list.

- ``drop_membership_dedup``: ``x IN (SELECT DISTINCT y ...)`` is ``x IN (SELECT y ...)``; a membership
  or existence test only sees which values occur, never how often.
- ``merge_grouped_source``: ``SELECT DISTINCT k FROM (SELECT k, COUNT(x) AS c ... GROUP BY k, j) AS d
  WHERE c > 1`` is ``SELECT DISTINCT k ... GROUP BY k, j HAVING COUNT(x) > 1``.
- ``drop_dedup_read_as_set``: a ``DISTINCT`` (or key-only ``GROUP BY``) derived table read by a query
  that only keeps distinct rows (through outer joins too) need not remove repeats itself.
- ``distinct_join_to_exists``: an inner join with a ``DISTINCT`` derived table equated on all of its
  columns, and read nowhere else, matches each row at most once: it is an ``EXISTS`` test.
- ``unwrap_column_parens``: ``SELECT DISTINCT(a)`` and ``COUNT(DISTINCT(a))`` read ``a``.
- ``drop_distinct_over_group_keys``: a grouped select that outputs every group key has no repeated rows.
- ``regroup_distinct``: an aggregate over ``(SELECT [k,] x, partials GROUP BY [k,] x)`` regrouped by
  ``k`` (or not grouped at all) is one aggregate with ``SUM(DISTINCT x)`` / ``COUNT(DISTINCT x)``.
"""

from __future__ import annotations

from sqlglot import exp


def _plain_distinct(select: exp.Select) -> bool:
    distinct = select.args.get("distinct")
    return distinct is not None and not distinct.args.get("on")


def _membership_query(select: exp.Select) -> bool:
    """True when ``select`` is the whole query of an ``IN (...)`` or ``EXISTS (...)`` test."""

    parent = select.parent
    if isinstance(parent, exp.Exists):
        return parent.this is select
    if isinstance(parent, exp.Subquery) and not parent.alias:
        holder = parent.parent
        if isinstance(holder, exp.In):
            return holder.args.get("query") is parent
        if isinstance(holder, exp.Exists):
            return holder.this is parent
    return False


def drop_membership_dedup(select: exp.Select) -> exp.Select | None:
    """Drop a ``DISTINCT`` (or a ``GROUP BY`` of exactly the selected columns) inside a membership test.

    ``IN`` is true when some row matches, NULL when none matches but some compare unknown, and false
    otherwise; ``EXISTS`` is true when there is a row. Each depends only on the set of rows, which
    ``DISTINCT`` keeps. A ``LIMIT``/``OFFSET`` or window would see the duplicates, so those block it.
    """

    if not _membership_query(select):
        return None
    if any(select.args.get(k) for k in ("limit", "offset", "qualify", "windows", "with", "with_")):
        return None
    if any(w.find_ancestor(exp.Select) is select for w in select.find_all(exp.Window)):
        return None
    if _plain_distinct(select):
        result = select.copy()
        result.set("distinct", None)
        return result
    group = select.args.get("group")
    if group is None or select.args.get("having") is not None or select.args.get("distinct") is not None:
        return None
    if any(group.args.get(k) for k in ("rollup", "cube", "grouping_sets", "totals")) or not group.expressions:
        return None
    if any(a.find_ancestor(exp.Select) is select for a in select.find_all(exp.AggFunc)):
        return None
    outputs = {(e.this if isinstance(e, exp.Alias) else e).sql() for e in select.expressions}
    if outputs != {g.sql() for g in group.expressions}:
        return None
    result = select.copy()
    result.set("group", None)
    return result


def _from_source(select: exp.Select) -> exp.Expression | None:
    from_ = select.args.get("from_") or select.args.get("from")
    return from_.this if from_ is not None else None


def _own(select: exp.Select, kinds) -> list[exp.Expression]:
    """Nodes of ``kinds`` in ``select`` that belong to it, not to a nested select."""

    return [n for n in select.find_all(kinds) if n.find_ancestor(exp.Select) is select]


def _set_output_group(select: exp.Select) -> bool:
    """A ``GROUP BY`` of exactly the selected columns with no aggregate: the same rows as ``DISTINCT``."""

    group = select.args.get("group")
    if group is None or select.args.get("having") is not None or not group.expressions:
        return False
    if any(group.args.get(k) for k in ("rollup", "cube", "grouping_sets", "totals")):
        return False
    if _own(select, exp.AggFunc):
        return False
    outputs = {(e.this if isinstance(e, exp.Alias) else e).sql().lower() for e in select.expressions}
    return outputs == {g.sql().lower() for g in group.expressions}


def _reads_output_alias(select: exp.Select) -> bool:
    """``GROUP BY``, ``HAVING`` or ``ORDER BY`` names an output alias (``SELECT a AS n ... GROUP BY n``).

    Rules that replace the select list would leave such a name pointing nowhere, or at a column.
    """

    renamed = {
        e.alias.lower() for e in select.expressions
        if isinstance(e, exp.Alias) and e.alias and not (isinstance(e.this, exp.Column) and e.this.name.lower() == e.alias.lower())
    }
    for clause in ("group", "having", "order"):
        node = select.args.get(clause)
        if node is not None and any(not c.table and c.name.lower() in renamed for c in node.find_all(exp.Column)):
            return True
    return False


def _grouped_outputs(inner: exp.Select, outputs: dict[str, exp.Expression]) -> bool:
    """Every output of a grouped select is fixed per group: columns outside aggregates are group keys.

    MySQL accepts ``SELECT k, x ... GROUP BY k`` and picks any ``x`` per group; such a value read twice
    need not be the same, so it is not substituted.
    """

    keys = set()
    for key in inner.args["group"].expressions:
        keys.add(key.sql().lower())
        if isinstance(key, exp.Column) and not key.table and key.name.lower() in outputs:
            keys.add(outputs[key.name.lower()].sql().lower())
    for value in outputs.values():
        if value.sql().lower() in keys:
            continue
        for column in value.find_all(exp.Column):
            if column.find_ancestor(exp.AggFunc) is None and column.sql().lower() not in keys:
                return False
    return True


def merge_grouped_source(select: exp.Select) -> exp.Select | None:
    """Fold a plain select over one grouped derived table into that grouped select.

    Each row of a grouped derived table is one group, so ``SELECT e FROM (grouped) AS d WHERE p`` keeps
    one row per group that passes ``p``: that is the grouped select with ``p`` added to its ``HAVING``
    and ``e`` as its output, with every ``d.c`` read as the expression ``c`` names inside. Only a
    ``DISTINCT`` (or set-output ``GROUP BY``) outer select is folded, which is the shape the prover
    cannot match against the single grouped form; its ``ORDER BY`` must name outputs or ``d`` columns.
    """

    if not (_plain_distinct(select) or _set_output_group(select)):
        return None
    if any(select.args.get(k) for k in ("limit", "offset", "qualify", "windows", "with", "with_", "joins", "laterals", "pivots")):
        return None
    if _own(select, (exp.AggFunc, exp.Window, exp.Star)) or select.args.get("having") is not None:
        return None
    source = _from_source(select)
    if not isinstance(source, exp.Subquery) or not source.alias or not isinstance(source.this, exp.Select):
        return None
    if source.args.get("alias") is not None and source.args["alias"].columns:
        return None
    inner = source.this
    group = inner.args.get("group")
    if group is None or not group.expressions or any(group.args.get(k) for k in ("rollup", "cube", "grouping_sets", "totals")):
        return None
    if any(inner.args.get(k) for k in ("limit", "offset", "qualify", "windows", "with", "with_", "order")):
        return None
    if _own(inner, exp.Window) or any(isinstance(n, (exp.Rand, exp.Star)) for e in inner.expressions for n in e.walk()):
        return None
    distinct = inner.args.get("distinct")
    if distinct is not None and distinct.args.get("on"):
        return None
    # Nothing in the outer select may be a nested query: its columns could name the derived table.
    parts = list(select.expressions) + [select.args.get(k) for k in ("where", "group", "order")]
    if any(isinstance(n, (exp.Select, exp.Subquery)) for part in parts if part is not None for n in part.walk()):
        return None
    outputs: dict[str, exp.Expression] = {}
    for item in inner.expressions:
        name = item.alias_or_name
        if not name or name.lower() in outputs:
            return None
        outputs[name.lower()] = item.this if isinstance(item, exp.Alias) else item
    if not _grouped_outputs(inner, outputs) or _reads_output_alias(inner):
        return None
    alias = source.alias.lower()

    def substitute(node: exp.Expression) -> exp.Expression:
        if isinstance(node, exp.Column):
            if node.table and node.table.lower() != alias:
                raise LookupError(node.sql())
            value = outputs.get(node.name.lower())
            if value is None:
                raise LookupError(node.sql())
            return value.copy()
        return node

    try:
        items = []
        for item in select.expressions:
            value = (item.this if isinstance(item, exp.Alias) else item).transform(substitute)
            name = item.alias_or_name
            items.append(exp.alias_(value, name) if name else value)
        where = select.args.get("where")
        condition = where.this.transform(substitute) if where is not None else None
        order = select.args.get("order")
        names = {item.alias_or_name.lower() for item in select.expressions if item.alias_or_name}
        if order is not None:
            order = order.copy()
            for ordered in order.expressions:
                key = ordered.this
                if isinstance(key, exp.Literal) or (isinstance(key, exp.Column) and not key.table and key.name.lower() in names):
                    continue
                ordered.set("this", key.transform(substitute))
    except LookupError:
        return None
    result = inner.copy()
    result.set("expressions", items)
    result.set("distinct", exp.Distinct())
    if condition is not None:
        having = result.args.get("having")
        combined = condition if having is None else exp.and_(exp.Paren(this=having.this.copy()), exp.Paren(this=condition))
        result.set("having", exp.Having(this=combined))
    result.set("order", order)
    return result


def _dedup(select: exp.Select) -> bool:
    """``select`` only removes repeated rows: a plain ``DISTINCT`` or a ``GROUP BY`` of exactly its outputs."""

    if any(select.args.get(k) for k in ("limit", "offset", "qualify", "windows", "order", "with", "with_")):
        return False
    if _own(select, (exp.Window, exp.AggFunc)) or any(isinstance(e, exp.Star) for e in select.expressions):
        return False
    if _plain_distinct(select):
        return select.args.get("group") is None and select.args.get("having") is None
    return _set_output_group(select)


def _insensitive(call: exp.Expression) -> bool:
    return isinstance(call, (exp.Min, exp.Max)) or isinstance(call.this, exp.Distinct) or bool(call.args.get("distinct"))


def _reader(select: exp.Expression) -> tuple[exp.Select, exp.Expression] | None:
    """The select that reads ``select`` as a derived table (through ``UNION ALL`` branches), and that source."""

    node = select
    while True:
        parent = node.parent
        if isinstance(parent, exp.Subquery) and not parent.alias and isinstance(parent.parent, exp.Union):
            node = parent
        elif isinstance(parent, exp.Union) and not parent.args.get("distinct") and type(parent) is exp.Union:
            node = parent
        else:
            break
    parent = node.parent
    if not isinstance(parent, exp.Subquery) or not parent.alias or not isinstance(parent.parent, (exp.From, exp.Join)):
        return None
    if parent.parent.parent is None or not isinstance(parent.parent.parent, exp.Select):
        return None
    if isinstance(parent.parent, exp.Join) and parent.parent.args.get("kind") and parent.parent.args["kind"].upper() in ("SEMI", "ANTI"):
        return None
    return parent.parent.parent, parent


def _reads_as_set(select: exp.Select, depth: int = 0) -> bool:
    """Only the set of ``select``'s source rows matters: repeats never change what its readers see.

    True for a ``DISTINCT`` select, a grouped or aggregate select whose aggregates ignore repeats
    (``MIN``, ``MAX``, ``DISTINCT`` ones), a membership test, and a plain select-project-join whose own
    rows are read that way. Inner and outer joins alike keep this: whether a row finds a partner
    depends only on which rows exist.
    """

    if depth > 16 or any(select.args.get(k) for k in ("limit", "offset", "qualify", "windows")) or _own(select, exp.Window):
        return False
    distinct = select.args.get("distinct")
    if distinct is not None:
        return not distinct.args.get("on")
    aggregates = _own(select, exp.AggFunc)
    group = select.args.get("group")
    if group is not None and any(group.args.get(k) for k in ("rollup", "cube", "grouping_sets", "totals")):
        return False
    if group is not None or aggregates:
        return all(_insensitive(a) for a in aggregates)
    if _membership_query(select):
        return True
    reader = _reader(select)
    return reader is not None and _reads_as_set(reader[0], depth + 1)


def _whole_table(select: exp.Select, schema: dict[str, list[str]] | None) -> bool:
    """``select`` lists every column of one base table, unfiltered: ``SELECT DISTINCT * FROM t``."""

    source = _from_source(select)
    if not schema or not isinstance(source, exp.Table) or select.args.get("joins") or select.args.get("where") is not None:
        return False
    columns = {t.lower(): {c.lower() for c in cs} for t, cs in schema.items()}.get(source.name.lower())
    alias = (source.alias_or_name or "").lower()
    names = []
    for item in select.expressions:
        value = item.this if isinstance(item, exp.Alias) else item
        if not isinstance(value, exp.Column) or (value.table and value.table.lower() != alias) or item.alias_or_name.lower() != value.name.lower():
            return False
        names.append(value.name.lower())
    return columns is not None and len(names) == len(set(names)) and set(names) == columns


def drop_dedup_read_as_set(select: exp.Select, schema: dict[str, list[str]] | None = None) -> exp.Select | None:
    """Drop the ``DISTINCT`` (or key-only ``GROUP BY``) of a derived table whose reader keeps only distinct rows.

    ``SELECT d2.b FROM (SELECT DISTINCT a FROM t) AS d1 LEFT JOIN (SELECT a, b FROM s GROUP BY a, b)
    AS d2 ON d1.a = d2.a GROUP BY d2.b`` is the same with plain ``(SELECT a FROM t)`` and ``(SELECT a, b
    FROM s)``: every join result row is made of source rows, a row of an outer join's preserved side
    gets NULLs exactly when no partner row exists, and both depend only on which rows exist.

    A ``DISTINCT`` or key-only ``GROUP BY`` reader whose joins are all inner is left alone:
    ``_push_distinct_into_sources`` puts ``DISTINCT`` sources there on purpose, and dropping them would
    undo it. A reader with ``MIN``/``MAX``/``DISTINCT`` aggregates is not that rule's shape, and neither is
    a ``SELECT DISTINCT *`` of one whole table: that rule never builds it (it projects the columns read,
    and leaves a table alone when every column is read), so dropping it cannot undo the other.
    """

    if not _dedup(select):
        return None
    found = _reader(select)
    if found is None:
        return None
    reader, _source = found
    joins = reader.args.get("joins") or []
    outer = any((j.args.get("side") or "").upper() in ("LEFT", "RIGHT", "FULL") for j in joins)
    if joins and not outer and not _own(reader, exp.AggFunc) and not _whole_table(select, schema):
        return None
    if not _reads_as_set(reader):
        return None
    result = select.copy()
    result.set("distinct", None)
    result.set("group", None)
    return result


def _conjuncts(node: exp.Expression | None) -> list[exp.Expression]:
    from .algebraic_equivalence import _conjuncts as split

    return split(node) if node is not None else []


def _and_all(parts: list[exp.Expression]) -> exp.Expression | None:
    from .algebraic_equivalence import _and_all as join

    return join([p.copy() for p in parts])


def _reads(node: exp.Expression, alias: str) -> bool:
    return any(c.table and c.table.lower() == alias for c in node.find_all(exp.Column))


def distinct_join_to_exists(select: exp.Select) -> exp.Select | None:
    """``FROM a JOIN (SELECT DISTINCT k FROM t WHERE p) AS d ON a.x = d.k`` is ``FROM a WHERE EXISTS (..)``.

    The derived table has no repeated rows, and every one of its columns is equated with a value of the
    other sources, so a row of the others matches at most one of its rows: the join keeps that row once
    when a match exists and drops it otherwise. When nothing else reads the derived table, the join is
    ``EXISTS (SELECT 1 FROM (SELECT k FROM t WHERE p) AS d WHERE <its join conditions>)``. Only inner
    joins are rewritten; every condition naming the derived table moves into the test.
    """

    joins = select.args.get("joins") or []
    if not joins or any(select.args.get(k) for k in ("laterals", "pivots", "connect", "match")):
        return None
    for join in joins:
        if join.args.get("side") or (join.args.get("kind") or "").upper() not in ("", "INNER", "CROSS"):
            return None
        if join.args.get("using") is not None or join.args.get("method") or join.args.get("global_"):
            return None
    sources = [_from_source(select)] + [j.this for j in joins]
    aliases = [(s_.alias_or_name or "").lower() if isinstance(s_, (exp.Table, exp.Subquery)) else "" for s_ in sources]
    if "" in aliases or len(set(aliases)) != len(aliases):
        return None
    # Every column of this select must say which source it reads.
    if any(not c.table for c in select.find_all(exp.Column) if c.find_ancestor(exp.Select) is select and not isinstance(c.this, exp.Star)):
        return None
    if any(isinstance(n, exp.Star) for e in select.expressions for n in e.walk()):
        return None
    where = select.args.get("where")
    for position, source in enumerate(sources):
        if not isinstance(source, exp.Subquery) or not isinstance(source.this, exp.Select) or not _dedup(source.this):
            continue
        if source.args.get("alias") is not None and source.args["alias"].columns:
            continue
        alias = aliases[position]
        names = [(e.alias_or_name or "").lower() for e in source.this.expressions]
        if "" in names or len(set(names)) != len(names):
            continue
        conditions = [(None, part) for part in _conjuncts(where.this if where is not None else None)]
        for join in joins:
            conditions += [(join, part) for part in _conjuncts(join.args.get("on"))]
        tests = [(owner, part) for owner, part in conditions if _reads(part, alias)]
        # Nothing outside those conditions may read the derived table (including nested subqueries).
        outside = [
            c for c in select.find_all(exp.Column)
            if c.table and c.table.lower() == alias and not any(_inside(c, part) for _, part in tests)
            and not _inside(c, source)
        ]
        if outside:
            continue
        # A nested query's bare column could resolve to the derived table by name.
        if any(not c.table and c.name.lower() in names and not _inside(c, source) for c in select.find_all(exp.Column)):
            continue
        equated = set()
        for _, part in tests:
            if not isinstance(part, exp.EQ):
                continue
            for mine, other in ((part.this, part.expression), (part.expression, part.this)):
                if (
                    isinstance(mine, exp.Column) and mine.table and mine.table.lower() == alias
                    and not _reads(other, alias)
                    and not any(isinstance(n, (exp.Subquery, exp.Select, exp.AggFunc, exp.Window, exp.Rand, exp.Anonymous)) for n in other.walk())
                ):
                    equated.add(mine.name.lower())
        if set(names) - equated:
            continue
        inner = source.this.copy()
        inner.set("distinct", None)
        inner.set("group", None)
        test = exp.Exists(this=exp.select(exp.Literal.number(1)).from_(exp.Subquery(this=inner, alias=source.args["alias"].copy())).where(_and_all([p for _, p in tests])))
        result = select.copy()
        new_joins = [j.copy() for j in joins]
        moved = [p for owner, p in conditions if owner is None and not _reads(p, alias)]
        kept_on = []
        for join, new_join in zip(joins, new_joins):
            parts = [p for owner, p in conditions if owner is join and not _reads(p, alias)]
            kept_on.append(parts)
        if position == 0:
            head = new_joins.pop(0)
            moved += kept_on.pop(0)
            result.set("from_" if "from_" in select.args or select.args.get("from_") is not None else "from", exp.From(this=head.this))
        else:
            del new_joins[position - 1]
            del kept_on[position - 1]
        for new_join, parts in zip(new_joins, kept_on):
            new_join.set("on", _and_all(parts))
            if not parts and (new_join.args.get("kind") or "").upper() == "INNER":
                new_join.set("kind", "CROSS")
        result.set("joins", new_joins or None)
        result.set("where", exp.Where(this=_and_all(moved + [test])))
        return result
    return None


def _inside(node: exp.Expression, root: exp.Expression) -> bool:
    while node is not None:
        if node is root:
            return True
        node = node.parent
    return False


_PARTIAL = (exp.Sum, exp.Count, exp.Min, exp.Max)


def _partial(node: exp.Expression) -> bool:
    return isinstance(node, _PARTIAL) and not node.args.get("distinct") and not isinstance(node.this, exp.Distinct)


def _zero(node: exp.Expression) -> bool:
    return isinstance(node, exp.Literal) and not node.is_string and node.name in ("0", "0.0")


def _fixed_by(inner: exp.Select, key_sql: str) -> bool:
    """A ``key = constant`` conjunct of the select's ``WHERE`` or ``HAVING`` fixes the group key."""

    for clause in ("where", "having"):
        node = inner.args.get(clause)
        for part in _conjuncts(node.this if node is not None else None):
            if isinstance(part, exp.EQ):
                for key, value in ((part.this, part.expression), (part.expression, part.this)):
                    if isinstance(key, exp.Column) and key.sql() == key_sql and isinstance(value, exp.Literal):
                        return True
    return False


def regroup_distinct(select: exp.Select) -> exp.Select | None:
    """Fold an aggregate over a finer grouping into one aggregate.

    ``SELECT [k,] f(..) FROM (SELECT [k,] x, SUM(c) AS p, COUNT(*) AS n FROM t GROUP BY [k,] x) AS d
    [GROUP BY k]`` has one inner row per distinct ``(k, x)``, so over them ``SUM(x)``, ``COUNT(x)``
    and ``AVG(x)`` see each value once (``SUM(DISTINCT x)`` etc., NULLs ignored by both), ``MIN``/``MAX``
    of ``x`` are unchanged, ``SUM(p)``, ``MIN(MIN)`` and ``MAX(MAX)`` of a partial are the aggregate
    over all rows, and ``COALESCE(SUM(n), 0)`` is the count. A bare ``SUM(n)`` is the count only under
    ``GROUP BY`` (a group is never empty); a global aggregate over no rows gives NULL, not 0. Items may
    be any expression over such aggregates (``SUM(a) DIV COUNT(a)``, ``CAST(..)``).

    With several extra keys only partials are re-aggregated. An extra key fixed to one constant by a
    ``key = constant`` filter (``HAVING deptno = 100``) does not split an outer group, so it stays in the
    ``GROUP BY`` with the inner ``HAVING``; otherwise an inner ``HAVING`` blocks the rewrite.
    """

    if any(select.args.get(k) for k in ("distinct", "where", "joins", "having", "limit", "offset", "qualify", "windows", "with", "with_", "laterals", "pivots", "order")):
        return None
    source = _from_source(select)
    if any(n is not source for n in _own(select, (exp.Window, exp.Star, exp.Subquery))):
        return None
    if not isinstance(source, exp.Subquery) or not source.alias or not isinstance(source.this, exp.Select):
        return None
    if source.args.get("alias") is not None and source.args["alias"].columns:
        return None
    inner = source.this
    group = inner.args.get("group")
    if group is None or any(group.args.get(k) for k in ("rollup", "cube", "grouping_sets", "totals")):
        return None
    if any(inner.args.get(k) for k in ("distinct", "limit", "offset", "qualify", "windows", "with", "with_", "order")) or _own(inner, exp.Window):
        return None
    inner_keys = list(group.expressions)
    if not inner_keys or any(not isinstance(k, exp.Column) for k in inner_keys) or _reads_output_alias(inner):
        return None
    key_sql = {k.sql() for k in inner_keys}
    keys: dict[str, exp.Expression] = {}
    partials: dict[str, exp.Expression] = {}
    for item in inner.expressions:
        value = item.this if isinstance(item, exp.Alias) else item
        name = (item.alias_or_name or "").lower()
        if isinstance(value, (exp.Null, exp.Literal)):
            continue  # a constant (a grouping-set branch's NULL); an outer read of it is refused below
        if not name or name in keys or name in partials:
            return None
        if _partial(value) and not any(isinstance(n, (exp.AggFunc, exp.Subquery, exp.Window)) for n in value.this.walk() if n is not value.this) and not isinstance(value.this, exp.AggFunc):
            partials[name] = value
        elif isinstance(value, exp.Column) and value.sql() in key_sql:
            keys[name] = value
        else:
            return None
    alias = source.alias.lower()

    def name_of(column: exp.Expression) -> str | None:
        if not isinstance(column, exp.Column) or (column.table and column.table.lower() != alias):
            return None
        return column.name.lower()

    outer_group = select.args.get("group")
    grouped = outer_group is not None
    outer_names: list[str] = []
    if grouped:
        if any(outer_group.args.get(k) for k in ("rollup", "cube", "grouping_sets", "totals")):
            return None
        for key in outer_group.expressions:
            name = name_of(key)
            if name not in keys:
                return None
            outer_names.append(name)
    extra = key_sql - {keys[n].sql() for n in outer_names}
    # An extra key fixed to one constant (``HAVING deptno = 100``) does not split an outer group.
    fixed = {k for k in extra if _fixed_by(inner, k)}
    extra -= fixed
    having = inner.args.get("having")
    if having is not None and extra:
        return None
    if fixed and (not grouped or extra):
        return None
    if not extra and not fixed:
        return None
    extra_names = [n for n, v in keys.items() if v.sql() in extra]
    extra_name = extra_names[0] if len(extra) == 1 and len(extra_names) == 1 else None
    x = keys[extra_name] if extra_name else None

    class Refuse(Exception):
        pass

    def aggregate(node: exp.Expression) -> exp.Expression:
        distinct = isinstance(node.this, exp.Distinct)
        arg = node.this.expressions[0] if distinct and len(node.this.expressions) == 1 else node.this
        name = name_of(arg)
        if name is None or (distinct and not isinstance(node, (exp.Sum, exp.Count, exp.Avg))):
            raise Refuse
        if name in keys and name not in outer_names and name != extra_name:
            raise Refuse
        if name == extra_name:
            if isinstance(node, (exp.Sum, exp.Count, exp.Avg)):
                return type(node)(this=exp.Distinct(expressions=[x.copy()]))
            if isinstance(node, (exp.Min, exp.Max)):
                return type(node)(this=x.copy())
            raise Refuse
        if distinct or name not in partials:
            raise Refuse
        partial = partials[name]
        if isinstance(node, exp.Sum) and isinstance(partial, exp.Sum):
            return partial.copy()
        if isinstance(node, exp.Sum) and isinstance(partial, exp.Count) and grouped:
            return partial.copy()
        if isinstance(node, (exp.Min, exp.Max)) and type(partial) is type(node):
            return partial.copy()
        raise Refuse

    def rewrite(node: exp.Expression) -> exp.Expression:
        if isinstance(node, exp.Coalesce) and len(node.expressions) == 1 and _zero(node.expressions[0]):
            inside = node.this
            if isinstance(inside, exp.Sum) and not isinstance(inside.this, exp.Distinct):
                name = name_of(inside.this)
                if name in partials and isinstance(partials[name], exp.Count):
                    return partials[name].copy()
        if isinstance(node, exp.AggFunc):
            if not isinstance(node, (exp.Sum, exp.Count, exp.Min, exp.Max, exp.Avg)) or node.args.get("distinct"):
                raise Refuse
            return aggregate(node)
        if isinstance(node, exp.Column):
            name = name_of(node)
            if name in outer_names:
                return keys[name].copy()
            raise Refuse
        return node

    def walk(node: exp.Expression) -> exp.Expression:
        replaced = rewrite(node)
        if replaced is not node:
            return replaced
        for key, value in list(node.args.items()):
            if isinstance(value, exp.Expression):
                node.set(key, walk(value))
            elif isinstance(value, list):
                node.set(key, [walk(v) if isinstance(v, exp.Expression) else v for v in value])
        return node

    items = []
    try:
        for item in select.expressions:
            value = (item.this if isinstance(item, exp.Alias) else item).copy()
            name = item.alias_or_name
            items.append(exp.alias_(walk(value), name) if name else walk(value))
    except Refuse:
        return None
    result = inner.copy()
    result.set("expressions", items)
    if grouped:
        kept = [keys[n].copy() for n in outer_names] + [k.copy() for k in inner_keys if k.sql() in fixed]
        result.set("group", exp.Group(expressions=kept))
    else:
        result.set("group", None)
    return result


def unwrap_column_parens(select: exp.Select) -> exp.Select | None:
    """``SELECT DISTINCT(a), COUNT(DISTINCT(b))`` is ``SELECT DISTINCT a, COUNT(DISTINCT b)``.

    MySQL reads the parentheses as grouping a column, not as a call; left in, they hide the column
    from rules that look for bare select items.
    """

    def column_paren(node: exp.Expression) -> bool:
        return isinstance(node, exp.Paren) and isinstance(node.this, exp.Column)

    own_distincts = [d for d in select.find_all(exp.Distinct) if d.find_ancestor(exp.Select) is select and isinstance(d.parent, exp.AggFunc)]
    items = [e.this if isinstance(e, exp.Alias) else e for e in select.expressions]
    if not any(column_paren(i) for i in items) and not any(column_paren(e) for d in own_distincts for e in d.expressions):
        return None
    result = select.copy()
    for position, item in enumerate(result.expressions):
        if isinstance(item, exp.Alias) and column_paren(item.this):
            item.set("this", item.this.this)
        elif column_paren(item):
            result.expressions[position] = item.this
            item.this.parent = result
    for distinct in [d for d in result.find_all(exp.Distinct) if d.find_ancestor(exp.Select) is result and isinstance(d.parent, exp.AggFunc)]:
        distinct.set("expressions", [e.this if column_paren(e) else e for e in distinct.expressions])
    return result


def drop_distinct_over_group_keys(select: exp.Select) -> exp.Select | None:
    """``SELECT DISTINCT k, j, COUNT(*) ... GROUP BY k, j`` is the same without ``DISTINCT``.

    Each group is one row and differs from every other group in some key (NULL keys group together, as
    ``DISTINCT`` compares them), so when every key is an output no two rows are the same.
    """

    if not _plain_distinct(select):
        return None
    group = select.args.get("group")
    if group is None or not group.expressions or any(group.args.get(k) for k in ("rollup", "cube", "grouping_sets", "totals")):
        return None
    if _own(select, exp.Window) or select.args.get("qualify"):
        return None
    outputs = {(e.this if isinstance(e, exp.Alias) else e).sql() for e in select.expressions}
    if any(g.sql() not in outputs for g in group.expressions):
        return None
    result = select.copy()
    result.set("distinct", None)
    return result


def fold_count_casts(select: exp.Select) -> exp.Select | None:
    """``CAST(COUNT(..) AS BIGINT)`` is the count: a count is already an integer (overflow is not modelled).

    Calcite writes this cast when it re-sums partial counts (``CAST(COALESCE(SUM(n), 0) AS BIGINT)``),
    and once ``regroup_distinct`` has turned the sum back into ``COUNT(*)`` it is the only difference left.
    """

    def is_count_cast(node: exp.Expression) -> bool:
        if not isinstance(node, exp.Cast) or isinstance(node, exp.TryCast) or not isinstance(node.this, exp.Count):
            return False
        to = node.args.get("to")
        return isinstance(to, exp.DataType) and to.is_type(exp.DataType.Type.BIGINT, exp.DataType.Type.INT) and not to.expressions

    if not any(is_count_cast(n) for n in _own(select, exp.Cast)):
        return None
    result = select.copy()
    for cast in [n for n in _own(result, exp.Cast) if is_count_cast(n)]:
        cast.replace(cast.this)
    return result


_RULES = (drop_membership_dedup, drop_dedup_read_as_set, merge_grouped_source, distinct_join_to_exists, regroup_distinct, unwrap_column_parens, drop_distinct_over_group_keys, fold_count_casts)


def distinct_rules(select: exp.Select, schema: dict[str, list[str]] | None = None) -> exp.Expression | None:
    """The first of this module's rules that rewrites ``select``, or None."""

    for rule in _RULES:
        rewritten = rule(select, schema) if rule is drop_dedup_read_as_set else rule(select)
        if rewritten is not None:
            return rewritten
    return None
