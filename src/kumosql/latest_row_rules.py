"""The row with the first (or last) value of each group, written as a join to the grouped ``MIN``/``MAX``.

"Latest row per key" has many spellings; this rule reads five of them and writes one, the join of the table
to ``SELECT key, MAX(order) FROM table GROUP BY key`` on the key and the order value. That is the form the
prover already proves equal to the correlated-subquery spelling (``o = (SELECT MAX(o) FROM t u WHERE u.k = t.k)``).

Readings (each over one table, an optional deterministic ``WHERE``, no joins, grouping or ``DISTINCT``):

* ``QUALIFY ROW_NUMBER() OVER (PARTITION BY k ORDER BY o) = 1`` (also ``RANK``, ``DENSE_RANK``; ``<= 1`` and
  ``< 2``), with the other ``QUALIFY`` conditions kept as ``WHERE`` conditions on the joined row;
* the same window as a column of a derived table that the outer query keeps only where it is 1
  (``SELECT .. FROM (SELECT .., ROW_NUMBER() OVER (..) AS rn FROM t) AS d WHERE d.rn = 1``): the derived
  table becomes the join and ``rn`` the literal 1;
* ``SELECT k, ARRAY_AGG(x ORDER BY o [DESC] LIMIT 1)[OFFSET(0)] .. GROUP BY k`` (``ORDINAL(1)`` and the
  ``SAFE_`` forms too);
* ``SELECT k, MAX_BY(x, o) .. GROUP BY k`` and ``MIN_BY(x, o)``;
* ``WHERE (k, o) IN (SELECT k, MAX(o) FROM t [WHERE w] GROUP BY k)``, a join already (``IN`` compares with ``=``,
  as the join does, so only the shape is checked).

A select that reads only the partition and order expressions (``SELECT k, o``) is written as the grouping itself,
``SELECT k, MAX(o) .. GROUP BY k``: for ``ROW_NUMBER`` whichever tied row is kept looks the same, for
``RANK``/``DENSE_RANK`` only when the order is total (they keep every tied row, a grouping one).

Written as ``SELECT .. FROM t JOIN (SELECT k AS kqp0, MAX(o) AS kqm FROM t [WHERE w] GROUP BY k) AS g ON
t.k <=> g.kqp0 AND t.o <=> g.kqm [WHERE w]``, with ``=`` where the column cannot be NULL and ``MIN`` for an
ascending order, ``MAX`` for a descending one.

Preconditions, each checked (the rule declines when one cannot be shown, and the negative tests pin each):

1. **One ordering key** ``o``, no frame. The join matches the one extreme value of ``o``.
2. **NULL order.** ``MIN``/``MAX`` skip NULL. An order that puts NULL *first* (BigQuery and MySQL ascending, or
   an explicit ``NULLS FIRST``) needs ``o`` provably NOT NULL (a declared NOT NULL column, or a ``WHERE``
   condition that rejects NULL). An order that puts NULL last (descending by default, or ``NULLS LAST``) is
   exact without it: a group with a non-NULL value has its extreme there, and a group of only NULLs has
   ``MAX`` = NULL, which the null-safe ``o <=> g.kqm`` matches. ``MAX_BY``/``MIN_BY`` skip rows whose ordering
   value is NULL and return NULL for a group of only NULLs, so they always need ``o`` NOT NULL.
3. **Ties.** ``RANK`` and ``DENSE_RANK`` give every peer of the first row rank 1, and the join keeps every row
   whose ``o`` is the extreme, so they need no key. ``ROW_NUMBER = 1``, ``ARRAY_AGG(.. LIMIT 1)[OFFSET(0)]`` and
   ``MAX_BY``/``MIN_BY`` pick *one* row of the peers, so the order must be total: the ``PARTITION BY`` (or
   ``GROUP BY``) and ``o`` expressions are unique over the rows read, by a declared key (``keys=`` with
   ``not_null=``) or whatever :mod:`kumosql.output_properties` can show unique. Otherwise the join would return
   every tied row where the window returns one, and nothing is rewritten.
4. **No floats.** A FLOAT64 ordering or partition key has NaN and signed zero, which ``ORDER BY`` and ``MAX``
   place differently (``MAX`` of a NaN is NaN, an ordering puts NaN first); a key that is, or computes, a float
   is refused. (A column of unknown type is taken as the prover takes it: never NaN.)
5. **Shape.** One plain table, no joins, no star in the select list, a ``WHERE`` without subqueries, windows,
   aggregates or random functions (it is evaluated twice), no ``HAVING``, no other window or aggregate, and
   in the grouped forms every non-aggregate output built from the ``GROUP BY`` expressions only.
6. ``ARRAY_AGG`` with ``IGNORE NULLS`` or ``DISTINCT`` is left alone (a different value), and so is a grouped
   select with no ``GROUP BY`` or a window with no ``PARTITION BY`` (a global aggregate returns a row on empty
   input; a window and the join do not).
7. ``MAX_BY``/``MIN_BY`` also need the picked value ``x`` NOT NULL: DuckDB's ``arg_max`` skips a row whose value is
   NULL, so an engine check could not vouch for the rewrite otherwise.

``ROW_NUMBER() ... = 1`` of a non-total order, a descending order with ``NULLS FIRST`` on a nullable key, and
``MAX_BY`` over a nullable key therefore stay as written.
"""

from __future__ import annotations

from sqlglot import exp

from .ast_utils import FROM_KEY, conjuncts
from .window_top_one import (
    Facts,
    Key,
    deterministic,
    may_be_float,
    numbering_window,
    one_row_operand,
    order_keys,
    owned_windows,
)

_TABLE_ARGS = {"this", "db", "catalog", "alias"}
_PLAIN_SELECT_BLOCKERS = ("group", "having", "distinct", "windows", "laterals", "pivots", "limit", "offset", "with_", "with", "qualify")


def latest_row_to_grouped_join(tree: exp.Expression, keys=None, not_null=None, schema=None, types=None, dialect: str = "bigquery") -> exp.Expression:
    """Rewrite every select of ``tree`` that reads one of the forms above (module doc), innermost first.

    ``keys`` and ``not_null`` are the declared facts ``normalize`` is given (table -> unique column tuples, table
    -> NOT NULL columns); ``types`` (table -> column -> type) lets a FLOAT64 key be refused."""

    if not any(isinstance(n, (exp.Window, exp.ArgMax, exp.ArgMin, exp.Bracket, exp.In)) for n in tree.walk()):
        return tree
    facts = Facts(keys, not_null, schema, types, dialect)
    counter = [0]
    for select in list(tree.find_all(exp.Select))[::-1]:
        replacement = _qualify_form(select, facts, counter) or _grouped_pick_form(select, facts, counter)
        if replacement is not None:
            if select is tree:
                tree = replacement
            else:
                select.replace(replacement)
            continue
        if not _derived_form(select, facts, counter):
            _membership_form(select, facts, counter)
    return tree


# --- the join -------------------------------------------------------------------------------


def _table(select: exp.Select) -> exp.Table | None:
    """The one plain table ``select`` reads, with no joins."""

    from_ = select.args.get(FROM_KEY)
    if from_ is None or select.args.get("joins") or not isinstance(from_.this, exp.Table):
        return None
    table = from_.this
    if any(table.args.get(k) for k in table.args if k not in _TABLE_ARGS) or table.this is None:
        return None
    if isinstance(table.this, exp.Func):  # a table function (UNNEST, GENERATE_ARRAY ..)
        return None
    return table


def _where_ok(select: exp.Select) -> bool:
    where = select.args.get("where")
    return where is None or deterministic(where.this)


def _qualified(node: exp.Expression, table: exp.Table) -> exp.Expression:
    """A copy of ``node`` whose bare columns name ``table`` (the one source in scope), so a select alias of the same
    name cannot capture them."""

    name = table.alias_or_name

    def name_it(piece: exp.Expression) -> exp.Expression:
        if isinstance(piece, exp.Column) and not piece.table and not isinstance(piece.this, exp.Star):
            return exp.column(piece.this.copy(), table=name)
        return piece

    return node.copy().transform(name_it)


def _eq(left: exp.Expression, right: exp.Expression, non_null: bool) -> exp.Expression:
    return exp.EQ(this=left, expression=right) if non_null else exp.NullSafeEQ(this=left, expression=right)


def _join_to_grouped(select: exp.Select, partitions: list[exp.Expression], key: Key, partition_non_null: list[bool], key_non_null: bool, number: int) -> exp.Join:
    """``JOIN (SELECT p AS kqp0, .., MAX(o) AS kqm FROM t [WHERE w] GROUP BY p, ..) AS g ON t.p <=> g.kqp0 AND ..``."""

    alias = f"kqlr{number}"
    items = [exp.alias_(p.copy(), f"kqp{i}") for i, p in enumerate(partitions)]
    items.append(exp.alias_((exp.Max if key.desc else exp.Min)(this=key.expr.copy()), "kqm"))
    grouped = exp.Select(expressions=items)
    grouped.set(FROM_KEY, exp.From(this=select.args[FROM_KEY].this.copy()))
    if select.args.get("where") is not None:
        grouped.set("where", select.args["where"].copy())
    if partitions:
        grouped.set("group", exp.Group(expressions=[p.copy() for p in partitions]))
    source = select.args[FROM_KEY].this
    conditions = [_eq(_qualified(p, source), exp.column(f"kqp{i}", table=alias), partition_non_null[i]) for i, p in enumerate(partitions)]
    conditions.append(_eq(_qualified(key.expr, source), exp.column("kqm", table=alias), key_non_null))
    return exp.Join(
        this=exp.Subquery(this=grouped, alias=exp.TableAlias(this=exp.to_identifier(alias))),
        on=exp.and_(*conditions),
    )


def _plan(select: exp.Select, partitions: list[exp.Expression], keys: list[Key] | None, facts: Facts, *, need_total: bool, need_non_null: bool, nulls_first: bool | None = None):
    """``(key, partition_non_null, key_non_null, unique)`` when the rewrite is exact for ``select`` (module doc), else None."""

    if keys is None or len(keys) != 1:
        return None
    key = keys[0]
    table = facts.table_of(select)
    if not all(deterministic(e) and not may_be_float(e, facts.types, table) for e in [*partitions, key.expr]):
        return None
    unique, non_null = facts.properties(select, [*partitions, key.expr])
    if need_total and not unique:
        return None
    first_nulls = key.nulls_first if nulls_first is None else nulls_first
    if (first_nulls or need_non_null) and not non_null[-1]:
        return None
    return key, non_null[:-1], non_null[-1], unique


def _shell(select: exp.Select) -> exp.Select:
    """``select`` reduced to the rows its grouping or window reads: FROM and WHERE."""

    shell = exp.Select(expressions=[exp.Star()])
    shell.set(FROM_KEY, select.args[FROM_KEY].copy())
    if select.args.get("where") is not None:
        shell.set("where", select.args["where"].copy())
    return shell


def _plain_source(select: exp.Select) -> bool:
    return _table(select) is not None and _where_ok(select) and not any(select.args.get(k) for k in _PLAIN_SELECT_BLOCKERS)


def _no_star(select: exp.Select) -> bool:
    return not any(isinstance(e, exp.Star) or (isinstance(e, exp.Column) and isinstance(e.this, exp.Star)) for e in select.expressions)


# --- ROW_NUMBER() OVER (..) = 1 ---------------------------------------------------------------


def _first_row_plan(select: exp.Select, window: exp.Window, facts: Facts):
    """The join plan for a numbering window that keeps its first row, or None."""

    if not numbering_window(window):
        return None
    partitions = list(window.args.get("partition_by") or [])
    keys = order_keys(window)
    row_number = isinstance(window.this, exp.RowNumber)
    shell = _shell(select)
    plan = _plan(shell, partitions, keys, facts, need_total=False, need_non_null=False)
    if plan is None:
        return None
    return partitions, plan, row_number


def _qualify_form(select: exp.Select, facts: Facts, counter: list[int]) -> exp.Select | None:
    qualify = select.args.get("qualify")
    if qualify is None or not _plain_source_with_qualify(select) or not _no_star(select):
        return None
    aliases = {i.alias.lower(): i.this for i in select.expressions if isinstance(i, exp.Alias)}
    chosen = None
    rest = []
    for part in conjuncts(qualify.this):
        operand = one_row_operand(part)
        window = None
        if isinstance(operand, exp.Window):
            window = operand
        elif isinstance(operand, exp.Column) and not operand.table and operand.name.lower() in aliases and isinstance(aliases[operand.name.lower()], exp.Window):
            window = aliases[operand.name.lower()]
        if window is not None and chosen is None and numbering_window(window):
            chosen = window
        else:
            rest.append(part)
    if chosen is None or any(r.find(exp.Window, exp.Subquery, exp.AggFunc) for r in rest):
        return None
    # a QUALIFY condition may name a select alias; a WHERE condition reads the table's column of that name instead
    if any(not c.table and c.name.lower() in aliases for r in rest for c in r.find_all(exp.Column)):
        return None
    marker = chosen.sql()
    others = [w for w in owned_windows(select) if w.sql() != marker]
    if others or any(isinstance(n, exp.AggFunc) and n.find_ancestor(exp.Window) is None for item in select.expressions for n in item.walk()):
        return None
    plan = _first_row_plan(select, chosen, facts)
    if plan is None:
        return None
    partitions, (key, partition_non_null, key_non_null, unique), row_number = plan
    items = [_swap_window(i, marker) for i in select.expressions]
    if (unique or row_number) and not rest and not any(select.args.get(k) for k in ("order", "limit", "offset")):
        grouped = _as_grouping(select, items, partitions, key)
        if grouped is not None:
            return grouped
    if row_number and not unique:
        return None
    counter[0] += 1
    join = _join_to_grouped(select, partitions, key, partition_non_null, key_non_null, counter[0])
    new = select.copy()
    new.set("qualify", None)
    new.set("expressions", [i.copy() for i in items])
    if new.args.get("order") is not None:
        new.set("order", exp.Order(expressions=[_swap_window(o, marker) for o in new.args["order"].expressions]))
    new.set("joins", [join])
    if rest:
        where = new.args.get("where")
        extra = exp.and_(*[r.copy() for r in rest])
        new.set("where", exp.Where(this=exp.and_(where.this, extra) if where is not None else extra))
    return new


def _as_grouping(select: exp.Select, items: list[exp.Expression], partitions: list[exp.Expression], key: Key) -> exp.Select | None:
    """``SELECT p, MAX(o) FROM t [WHERE w] GROUP BY p`` for a select that only reads the partition and order
    expressions of its rows. ``ROW_NUMBER = 1`` keeps one row per group, whichever of the tied rows, and a select
    that reads only ``p`` and ``o`` cannot tell them apart; ``RANK = 1`` keeps every tied row, so it needs a total
    order (each group's first row is its only row with that ``o``). Either way reading the first row's ``o`` is
    reading the group's extreme: the join to the grouped table would be the table itself."""

    table = _table(select)
    agg = exp.Max if key.desc else exp.Min
    key_sql, partition_sqls = key.expr.sql(), {p.sql() for p in partitions}

    def read(node: exp.Expression) -> exp.Expression | None:
        if node.sql() in partition_sqls:
            return node.copy()
        if node.sql() == key_sql:
            return agg(this=node.copy())
        if isinstance(node, exp.Column):
            return None
        children = {}
        for name, value in node.args.items():
            if isinstance(value, exp.Expression):
                child = read(value)
                if child is None:
                    return None
                children[name] = child
            elif isinstance(value, list):
                converted = []
                for entry in value:
                    child = read(entry) if isinstance(entry, exp.Expression) else entry
                    if child is None:
                        return None
                    converted.append(child)
                children[name] = converted
        rebuilt = node.copy()
        for name, child in children.items():
            rebuilt.set(name, child)
        return rebuilt

    # no partition: a global aggregate returns a row over no input where the window returns none
    if table is None or not partitions or any(i.find(exp.Subquery, exp.Window, exp.AggFunc) for i in items):
        return None
    new_items = []
    for item in items:
        read_item = read(item.this) if isinstance(item, exp.Alias) else read(item)
        if read_item is None:
            return None
        if isinstance(item, exp.Alias):
            new_items.append(exp.alias_(read_item, item.alias))
        elif read_item.sql() != item.sql() and item.alias_or_name:
            new_items.append(exp.alias_(read_item, item.alias_or_name))  # keep the output name (a derived table's readers use it)
        else:
            new_items.append(read_item)
    new = select.copy()
    new.set("expressions", new_items)
    new.set("qualify", None)
    new.set("group", exp.Group(expressions=[p.copy() for p in partitions]))
    return new


def _swap_window(item: exp.Expression, marker: str) -> exp.Expression:
    """``item`` with the window whose text is ``marker`` read as 1 (it is 1 on every row that is kept)."""

    def swap(node: exp.Expression) -> exp.Expression:
        return exp.Literal.number(1) if isinstance(node, exp.Window) and node.sql() == marker else node

    return item.transform(swap)


def _plain_source_with_qualify(select: exp.Select) -> bool:
    return _table(select) is not None and _where_ok(select) and not any(
        select.args.get(k) for k in _PLAIN_SELECT_BLOCKERS if k not in ("qualify", "limit", "offset")
    )


# --- SELECT .. FROM (SELECT .., ROW_NUMBER() OVER (..) AS rn FROM t) AS d WHERE d.rn = 1 ---------


def _derived_form(outer: exp.Select, facts: Facts, counter: list[int]) -> bool:
    where = outer.args.get("where")
    from_ = outer.args.get(FROM_KEY)
    if where is None or from_ is None:
        return False
    sources = [from_.this] + [j.this for j in outer.args.get("joins") or []]
    for part in conjuncts(where.this):
        operand = one_row_operand(part)
        if not isinstance(operand, exp.Column):
            continue
        matches = [s for s in sources if isinstance(s, exp.Subquery) and s.alias and (operand.table.lower() == s.alias.lower() or (not operand.table and len(sources) == 1))]
        if len(matches) != 1:
            continue
        inner = matches[0].this
        if not isinstance(inner, exp.Select) or not _plain_source(inner) or not _no_star(inner):
            continue
        windows = owned_windows(inner)
        items = [i for i in inner.expressions if isinstance(i, exp.Alias) and isinstance(i.this, exp.Window) and i.alias.lower() == operand.name.lower()]
        if len(windows) != 1 or len(items) != 1 or items[0].this is not windows[0]:
            continue
        if any(isinstance(n, exp.AggFunc) and n.find_ancestor(exp.Window) is None for i in inner.expressions for n in i.walk()):
            continue
        plan = _first_row_plan(inner, windows[0], facts)
        if plan is None:
            continue
        partitions, (key, partition_non_null, key_non_null, unique), row_number = plan
        items = [exp.alias_(exp.Literal.number(1), i.alias) if i.alias.lower() == operand.name.lower() else i.copy() for i in inner.expressions]
        new = _as_grouping(inner, items, partitions, key) if unique or row_number else None
        if new is None and row_number and not unique:
            continue
        if new is None:
            counter[0] += 1
            new = inner.copy()
            new.set("expressions", items)
            new.set("joins", [_join_to_grouped(inner, partitions, key, partition_non_null, key_non_null, counter[0])])
        inner.replace(new)
        return True
    return False


# --- WHERE (k, o) IN (SELECT k, MAX(o) FROM t GROUP BY k) -----------------------------------------


def _membership_form(select: exp.Select, facts: Facts, counter: list[int]) -> bool:
    """A top-level ``WHERE (l1, .., ln) IN (SELECT e1, .., en FROM t [WHERE w] GROUP BY p, ..)`` whose ``ei`` are
    group expressions or ``MIN``/``MAX`` calls, and in which every group expression is among the ``ei``, is a join to
    that grouped table on ``li = g.ci``: the grouped table has one row per value of its group expressions, and the
    ``li`` pin all of them, so a row of the outer select matches at most one of its rows (a join, not a semi-join, but
    the same rows). ``IN`` compares with ``=`` (NULL never matches), and so does the join."""

    where, from_ = select.args.get("where"), select.args.get(FROM_KEY)
    if where is None or from_ is None or select.args.get("joins") or any(select.args.get(k) for k in ("laterals", "pivots")):
        return False
    for part in conjuncts(where.this):
        if not isinstance(part, exp.In) or part.args.get("unnest") or part.args.get("is_global") or part.args.get("expressions"):
            continue
        query = part.args.get("query")
        sub = query.this if isinstance(query, exp.Subquery) else None
        lefts = list(part.this.expressions) if isinstance(part.this, exp.Tuple) else [part.this]
        if not isinstance(sub, exp.Select) or not _grouped_extremes(sub, facts) or len(sub.expressions) != len(lefts):
            continue
        if not all(deterministic(e) for e in lefts):
            continue
        counter[0] += 1
        alias = f"kqlr{counter[0]}"
        grouped = exp.Select(expressions=[exp.alias_(_unalias(i).copy(), f"kqc{n}") for n, i in enumerate(sub.expressions)])
        grouped.set(FROM_KEY, exp.From(this=sub.args[FROM_KEY].this.copy()))
        if sub.args.get("where") is not None:
            grouped.set("where", sub.args["where"].copy())
        grouped.set("group", sub.args["group"].copy())
        on = exp.and_(*[exp.EQ(this=left.copy(), expression=exp.column(f"kqc{n}", table=alias)) for n, left in enumerate(lefts)])
        rest = [c for c in conjuncts(where.this) if c is not part]
        select.set("where", exp.Where(this=exp.and_(*[c.copy() for c in rest])) if rest else None)
        select.set("joins", [exp.Join(this=exp.Subquery(this=grouped, alias=exp.TableAlias(this=exp.to_identifier(alias))), on=on)])
        return True
    return False


def _unalias(item: exp.Expression) -> exp.Expression:
    return item.this if isinstance(item, exp.Alias) else item


def _grouped_extremes(sub: exp.Select, facts: Facts) -> bool:
    """``sub`` is ``SELECT <group expressions and MIN/MAX calls> FROM t [WHERE w] GROUP BY <expressions>`` over one
    plain table, with every group expression selected and nothing in it correlated."""

    group = sub.args.get("group")
    table = _table(sub)
    if table is None or group is None or not group.expressions or any(group.args.get(k) for k in group.args if k != "expressions"):
        return False
    if any(sub.args.get(k) for k in ("having", "distinct", "windows", "qualify", "limit", "offset", "order", "laterals", "with_", "with")) or not _where_ok(sub):
        return False
    columns = facts.schema.get(table.name.lower())
    if columns is None:
        return False
    name = table.alias_or_name.lower()
    for column in sub.find_all(exp.Column):
        if (column.table and column.table.lower() != name) or (not column.table and column.name.lower() not in columns):
            return False
    if sub.find(exp.Subquery, exp.Window) or not _no_star(sub):
        return False
    keys = {e.sql() for e in group.expressions}
    if not all(deterministic(e) and not may_be_float(e, facts.types, table.name.lower()) for e in group.expressions):
        return False
    selected = set()
    for item in sub.expressions:
        inner = _unalias(item)
        if inner.sql() in keys:
            selected.add(inner.sql())
        elif isinstance(inner, (exp.Min, exp.Max)) and not inner.args.get("expressions") and inner.this is not None and deterministic(inner.this) and not isinstance(inner.this, exp.Distinct):
            continue
        else:
            return False
    return selected == keys


# --- ARRAY_AGG(x ORDER BY o LIMIT 1)[OFFSET(0)], MAX_BY(x, o), MIN_BY(x, o) -------------------


def _pick(node: exp.Expression, extreme: Key | None = None) -> tuple[exp.Expression, Key, bool] | None:
    """``(x, order key, skips NULL ordering values)`` for a call that picks ``x`` from the first row of a group.

    With ``extreme`` (the picks' order key), ``MAX(o)`` for a descending key and ``MIN(o)`` for an ascending one is
    a pick of ``o`` itself: the first row's own ordering value (NULL for a group of only NULLs, as the row has)."""

    if extreme is not None and isinstance(node, exp.Max if extreme.desc else exp.Min) and not node.args.get("expressions"):
        if node.this is not None and node.this.sql() == extreme.expr.sql():
            return node.this, extreme, False

    if isinstance(node, (exp.ArgMax, exp.ArgMin)):
        if node.args.get("count") is not None:
            return None
        value, order = node.this, node.expression
        if value is None or order is None:
            return None
        # MAX_BY puts the largest ordering value first, MIN_BY the smallest; NULL ordering values are skipped
        return value, Key(order, isinstance(node, exp.ArgMax), isinstance(node, exp.ArgMin)), True
    if isinstance(node, exp.Bracket) and isinstance(node.this, exp.ArrayAgg):
        if node.args.get("safe") not in (True, False, None) or len(node.expressions) != 1:
            return None
        index = node.expressions[0]
        offset = node.args.get("offset")
        if not (isinstance(index, exp.Literal) and not index.is_string and index.this.isdigit()) or offset is None:
            return None
        if int(index.this) - int(offset) != 0:
            return None
        call = node.this.this
        if not isinstance(call, exp.Limit) or not isinstance(call.expression, exp.Literal) or call.expression.this != "1" or call.args.get("offset"):
            return None
        ordered = call.this
        if not isinstance(ordered, exp.Order) or len(ordered.expressions) != 1 or not isinstance(ordered.expressions[0], exp.Ordered):
            return None
        value = ordered.this
        if value is None or isinstance(value, (exp.Distinct, exp.IgnoreNulls)):
            return None
        item = ordered.expressions[0]
        nulls_first = item.args.get("nulls_first")
        if nulls_first is None:
            return None
        return value, Key(item.this, bool(item.args.get("desc")), bool(nulls_first)), False
    return None


def _covered(node: exp.Expression, keys: set[str], picks: set[int], aliases: set[str]) -> bool:
    """Every column of ``node`` sits inside one of the ``GROUP BY`` expressions or a recognized pick (or names
    an output of the select, which the same test covers)."""

    if id(node) in picks or node.sql() in keys:
        return True
    if isinstance(node, exp.Column):
        return not node.table and node.name.lower() in aliases
    return all(_covered(child, keys, picks, aliases) for child in node.iter_expressions())


def _grouped_pick_form(select: exp.Select, facts: Facts, counter: list[int]) -> exp.Select | None:
    group = select.args.get("group")
    if group is None or select.args.get("having") is not None or not _no_star(select):
        return None
    if any(select.args.get(k) for k in ("distinct", "windows", "qualify", "laterals", "pivots", "with_", "with")) or not _where_ok(select):
        return None
    if _table(select) is None or any(group.args.get(k) for k in group.args if k != "expressions") or not group.expressions:
        return None
    partitions = list(group.expressions)
    picks = _find_picks(select, None)
    if not picks:
        return None
    picks = _find_picks(select, next(iter(picks.values()))[1])
    pick_ids = set(picks)
    key_sqls = {p.sql() for p in partitions}
    aliases = {i.alias.lower() for i in select.expressions if isinstance(i, exp.Alias)}
    for item in [*select.expressions, *(select.args["order"].expressions if select.args.get("order") else [])]:
        for node in item.walk():
            if isinstance(node, (exp.AggFunc, exp.Window, exp.Subquery)) and id(node) not in pick_ids and not any(
                id(a) in pick_ids for a in _ancestors(node)
            ):
                return None
        if not _covered(item, key_sqls, pick_ids, aliases):
            return None
    ordering = {(k.expr.sql(), k.desc, k.nulls_first) for _, k, _ in picks.values()}
    if len(ordering) != 1:
        return None
    skips_null = any(s for _, _, s in picks.values())
    values = [v for v, _, _ in picks.values()]
    if not all(deterministic(v) for v in values):
        return None
    key = next(iter(picks.values()))[1]
    plan = _plan(_shell(select), partitions, [key], facts, need_total=True, need_non_null=skips_null)
    if plan is None:
        return None
    needed = [v for v, _, skips in picks.values() if skips]
    if needed and not all(facts.properties(_shell(select), [v])[1][0] for v in needed):
        return None  # MAX_BY of a NULL value is read differently by BigQuery and DuckDB: the value must be NOT NULL too
    _, partition_non_null, key_non_null, _ = plan
    counter[0] += 1
    new = select.copy()
    new.set("expressions", [_replace_picks(i, _table(select), key) for i in new.expressions])
    new.set("group", None)
    new.set("joins", [_join_to_grouped(select, partitions, key, partition_non_null, key_non_null, counter[0])])
    return new


def _ancestors(node: exp.Expression):
    parent = node.parent
    while parent is not None:
        yield parent
        parent = parent.parent


def _find_picks(select: exp.Select, extreme: Key | None) -> dict[int, tuple[exp.Expression, Key, bool]]:
    picks = {}
    for item in select.expressions:
        for node in item.walk():
            found = _pick(node, extreme)
            if found is not None:
                picks[id(node)] = found
    return picks


def _replace_picks(item: exp.Expression, table: exp.Table, key: Key) -> exp.Expression:
    def swap(node: exp.Expression) -> exp.Expression:
        found = _pick(node, key)
        return _qualified(found[0], table) if found is not None else node

    return item.transform(swap, copy=False)
