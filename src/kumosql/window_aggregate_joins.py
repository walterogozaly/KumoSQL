"""A windowed aggregate over whole partitions, spelled as a join to the grouped aggregate.

``SELECT id, SUM(v) OVER (PARTITION BY k) AS s FROM t`` gives each row the sum over its partition. The same
numbers come from ``SELECT t.id, g.s FROM t JOIN (SELECT k, SUM(v) AS s FROM t GROUP BY k) AS g ON t.k = g.k``,
once the join treats the keys the way ``PARTITION BY`` and ``GROUP BY`` do. ``windowed_aggregate_joins``
rewrites the first into the second (the form a person writes by hand) when all of these hold, and otherwise
leaves the select alone:

* every window of the select is ``SUM``, ``COUNT`` (``COUNT(*)`` too), ``MIN``, ``MAX`` or ``AVG`` of an
  expression (no ``DISTINCT``) over a frame that is the whole partition: ``PARTITION BY`` keys and nothing
  else (``window_canonical`` has already dropped a full ``ROWS``/``RANGE`` frame and an ``ORDER BY`` that
  cannot matter). With an ``ORDER BY`` or an offset frame the value changes from row to row. A partial
  frame can also be empty, where ``SUM`` is NULL and ``COUNT`` is 0; the whole-partition frame always holds
  the row itself, so it is never empty and the group aggregate reads exactly the rows the window reads;
* the select reads one source (a table or a derived table), with an optional ``WHERE``, and has no ``GROUP BY``,
  ``HAVING``, ``QUALIFY`` or named ``WINDOW`` clause. Its source and ``WHERE`` are deterministic (no
  ``RAND``, clock or unknown function) and hold no subquery, so the grouped copy reads the same rows;
* the keys and arguments are plain expressions (no aggregate, window or subquery) and no key is known to be
  a floating-point column (``NaN`` and ``-0`` need care that a join does not give).

NULL keys form one partition, and a ``GROUP BY`` groups them into one group too, so the join must pair a NULL
key with the group of NULL keys: it is null-safe (``IS NOT DISTINCT FROM``) unless every key is a plain column
declared NOT NULL, where ``=`` says the same. Each row of the select finds exactly one group (its own key's),
so the join keeps every row once. Windows with no ``PARTITION BY`` become a one-row aggregate crossed in; an
empty input has no rows to carry the value. Every distinct list of keys gets one grouped derived table.

The rewrite runs only as a later attempt of the prover (``window_joins``), after the plain attempt fails.
"""

from __future__ import annotations

from sqlglot import exp

from .ast_utils import FROM_KEY

_AGGREGATES = (exp.Sum, exp.Count, exp.Min, exp.Max, exp.Avg)
_FLOAT_TYPES = ("FLOAT", "DOUBLE", "REAL", "FLOAT64", "FLOAT32")
_RUN_DEPENDENT = {"CurrentDatetime", "CurrentUser", "Randn", "Uuid", "Rand", "CurrentDate", "CurrentTime", "CurrentTimestamp", "TableSample"}
_UNSUPPORTED_CLAUSES = ("group", "having", "qualify", "windows", "with_", "with", "laterals", "pivots")


def windowed_aggregate_joins(
    tree: exp.Expression,
    not_null: dict[str, frozenset[str]] | None = None,
    types: dict[str, dict[str, str]] | None = None,
) -> exp.Expression:
    """Rewrite each eligible select of ``tree`` (module doc); returns the (possibly new) root."""

    for select in list(tree.find_all(exp.Select))[::-1]:
        rewritten = _rewrite(select, not_null or {}, types or {})
        if rewritten is not None:
            if select is tree:
                tree = rewritten
            else:
                select.replace(rewritten)
    return tree


def source_of(select: exp.Select) -> exp.Expression | None:
    """The one table or derived table ``select`` reads, or None (joins, laterals, table functions)."""

    source = select.args.get(FROM_KEY)
    if source is None or select.args.get("joins") or select.args.get("laterals"):
        return None
    this = source.this
    if isinstance(this, exp.Table) and not (this.args.get("pivots") or this.args.get("joins") or this.args.get("version")):
        return this
    if isinstance(this, exp.Subquery) and isinstance(this.this, exp.Select) and this.alias:
        return this
    return None


def deterministic(node: exp.Expression) -> bool:
    """No subquery, unknown function or run-dependent value under ``node``."""

    return not any(
        isinstance(n, (exp.Subquery, exp.Exists, exp.Anonymous, exp.Placeholder, exp.Parameter)) or type(n).__name__ in _RUN_DEPENDENT
        for n in node.walk()
    )


def _owned_windows(select: exp.Select) -> list[exp.Window]:
    return [w for w in select.find_all(exp.Window) if w.find_ancestor(exp.Select) is select]


def _plain(node: exp.Expression) -> bool:
    return not any(isinstance(n, (exp.AggFunc, exp.Window, exp.Subquery, exp.Select)) for n in node.walk()) and deterministic(node)


def _eligible(window: exp.Window) -> bool:
    function = window.this
    if not isinstance(function, _AGGREGATES) or window.args.get("alias") or window.args.get("first"):
        return False
    if window.args.get("order") is not None or window.args.get("spec") is not None:
        return False
    argument = function.this
    if argument is None or isinstance(argument, exp.Distinct) or function.expressions:
        return False
    if isinstance(argument, exp.Star):
        if not isinstance(function, exp.Count):
            return False
    elif not _plain(argument):
        return False
    return all(_plain(key) for key in window.args.get("partition_by") or [])


def _known_float(source: exp.Expression, key: exp.Expression, types: dict) -> bool:
    if not isinstance(source, exp.Table):
        return False
    names = (source.name.lower(), source.alias_or_name.lower())
    for column in key.find_all(exp.Column):
        if column.table and column.table.lower() not in names:
            continue
        kind = ((types.get(source.name.lower()) or {}).get(column.name.lower()) or "").upper()
        if kind.split("(")[0].strip() in _FLOAT_TYPES:
            return True
    return False


def _never_null(source: exp.Expression, keys: list[exp.Expression], not_null: dict) -> bool:
    if not isinstance(source, exp.Table):
        return False
    names = (source.name.lower(), source.alias_or_name.lower())
    declared = {c.lower() for c in not_null.get(source.name.lower(), frozenset())}
    return all(
        isinstance(k, exp.Column) and not isinstance(k.this, exp.Star) and k.name.lower() in declared and (not k.table or k.table.lower() in names)
        for k in keys
    )


def _rewrite(select: exp.Select, not_null: dict, types: dict) -> exp.Select | None:
    windows = _owned_windows(select)
    source = source_of(select)
    if not windows or source is None or any(select.args.get(k) for k in _UNSUPPORTED_CLAUSES):
        return None
    if select.args.get("distinct") is not None and select.args["distinct"].args.get("on"):
        return None
    if not all(_eligible(w) for w in windows):
        return None
    where = select.args.get("where")
    if where is not None and not deterministic(where):
        return None
    if isinstance(source, exp.Subquery) and not deterministic(source):
        return None
    for item in select.expressions:
        # a star would also read the grouped columns; an unnamed window has no name the join column could keep
        if isinstance(item, exp.Window) or any(isinstance(n, exp.Star) for n in item.walk() if n.find_ancestor(exp.Window) is None):
            return None
    groups: dict[str, list[exp.Window]] = {}
    keys_of: dict[str, list[exp.Expression]] = {}
    for window in windows:
        keys = list({k.sql(): k for k in window.args.get("partition_by") or []}.values())
        keys.sort(key=lambda k: k.sql())
        if any(_known_float(source, key, types) for key in keys):
            return None
        text = ", ".join(k.sql() for k in keys)
        groups.setdefault(text, []).append(window)
        keys_of[text] = keys
    swaps: dict[str, exp.Column] = {}
    joins = []
    for index, text in enumerate(sorted(groups)):
        keys, alias = keys_of[text], f"kqg{index}"
        items = [exp.alias_(key.copy(), f"kqk{n}") for n, key in enumerate(keys)]
        null_safe = not _never_null(source, keys, not_null)
        condition = None
        for n, key in enumerate(keys):
            right = exp.column(f"kqk{n}", table=alias)
            part = exp.NullSafeEQ(this=key.copy(), expression=right) if null_safe else exp.EQ(this=key.copy(), expression=right)
            condition = part if condition is None else exp.And(this=condition, expression=part)
        calls = sorted({w.this.sql() for w in groups[text]})
        for n, sql in enumerate(calls):
            items.append(exp.alias_(next(w for w in groups[text] if w.this.sql() == sql).this.copy(), f"kqa{n}"))
        for w in groups[text]:
            swaps[w.sql()] = exp.column(f"kqa{calls.index(w.this.sql())}", table=alias)
        grouped = exp.Select(expressions=items)
        grouped.set(FROM_KEY, select.args[FROM_KEY].copy())
        if where is not None:
            grouped.set("where", where.copy())
        if keys:
            grouped.set("group", exp.Group(expressions=[k.copy() for k in keys]))
        join = exp.Join(this=exp.Subquery(this=grouped, alias=exp.TableAlias(this=exp.to_identifier(alias))))
        if condition is None:
            join.set("kind", "CROSS")
        else:
            join.set("on", condition)
        joins.append(join)

    def swap(node: exp.Expression) -> exp.Expression:
        return swaps[node.sql()].copy() if isinstance(node, exp.Window) else node

    outer = select.copy()
    outer.set("expressions", [item.transform(swap) for item in outer.expressions])
    if outer.args.get("order") is not None:
        outer.set("order", outer.args["order"].transform(swap))
    outer.set("joins", joins)
    return outer
