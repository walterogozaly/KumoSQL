"""Move a filter on PARTITION BY columns below the windows of a derived table.

``SELECT user_id, rn FROM (SELECT user_id, ROW_NUMBER() OVER (PARTITION BY user_id ORDER BY ts, id) AS rn FROM t) WHERE user_id = 1``
is ``SELECT user_id, rn FROM (SELECT user_id, ROW_NUMBER() OVER (PARTITION BY user_id ORDER BY ts, id) AS rn FROM t WHERE user_id = 1)``.

A window sees the rows of its own partition and no others: its frame, its peers and its ties are all inside
one partition. A predicate on a column that every window partitions by has one value per partition (rows
whose keys are equal, NULLs included, share a partition), so it keeps or drops a partition whole. Dropping
whole partitions before the windows leaves every other row's window values unchanged, and the rows that
survive are the same ones. The pushed form is the normal form: a filter written below the window and one
written above it read alike. It holds on every database, ties included: removing other partitions does not
change the order of the rows inside a partition, and what a window does with a tie among them is untouched.

Preconditions (anything else is left as written):

* the select reads exactly one source, a derived table, with no join; the filter is the select's WHERE;
* the derived table is a plain select with at least one window, and no ``GROUP BY``, ``HAVING``, ``DISTINCT``,
  ``ORDER BY``, ``LIMIT``, ``OFFSET`` or ``WINDOW`` clause, no ``*`` in its select list, and no subquery in
  it (a ``QUALIFY`` is allowed: it keeps or drops rows by their own window values, partition by partition);
* every window of the derived table (select list and QUALIFY) has a ``PARTITION BY``;
* a conjunct of the WHERE moves only if it reads only output columns of the derived table that are plain
  columns of its source (not a window result, not an expression), and each such column is itself a
  ``PARTITION BY`` key of every window. A conjunct on any other column, on a window result, or one mixing in such a column
  stays above; the WHERE splits at ``AND``, so the movable conjuncts of a mixed WHERE still move;
* a moved conjunct is built only from column-to-column and column-to-literal comparisons, ``IN`` over literals,
  ``IS [NOT] NULL``, ``BETWEEN``, ``AND``, ``OR`` and ``NOT``. A function of a partition key is not moved:
  rows whose keys are equal can still differ (``-0.0`` and ``0.0`` share a FLOAT64 partition but ``1 / x``
  differs), a comparison cannot tell them apart.

The reverse spelling needs no rule of its own: both directions read as the pushed form. Pruning of unused
windows is ``window_canonical.prune_unread_windows`` and ``unread_windows.drop_unread_windows``, which run
beside this.
"""

from __future__ import annotations

from sqlglot import exp

from .ast_utils import FROM_KEY, conjuncts

_SHAPE = (exp.And, exp.Or, exp.Not, exp.Paren, exp.EQ, exp.NEQ, exp.GT, exp.GTE, exp.LT, exp.LTE, exp.In, exp.Is, exp.Between, exp.Column)


def _star(select: exp.Select) -> bool:
    return any(isinstance(s, exp.Star) or isinstance(s, exp.Column) and isinstance(s.this, exp.Star) for s in select.expressions)


def _constant(node: exp.Expression) -> bool:
    """A literal, NULL, TRUE/FALSE, a negated literal or a cast of a literal (``DATE '2024-01-01'``)."""

    if isinstance(node, (exp.Literal, exp.Null, exp.Boolean)):
        return True
    return isinstance(node, (exp.Neg, exp.Cast)) and isinstance(node.this, exp.Literal)


def _plain_shape(node: exp.Expression) -> bool:
    """Whether ``node`` uses only the comparison forms of the module doc."""

    if _constant(node):
        return True
    if not isinstance(node, _SHAPE):
        return False
    if isinstance(node, exp.Column):
        return not isinstance(node.this, exp.Star)
    if isinstance(node, exp.In):
        if node.args.get("query") is not None or node.args.get("unnest") is not None or node.args.get("field") is not None:
            return False
        if not node.expressions or not all(_constant(e) for e in node.expressions):
            return False
        return _plain_shape(node.this)
    return all(_plain_shape(child) for child in node.iter_expressions())


def _key(column: exp.Expression, source_names: set[str]) -> str:
    """A column's text with the inner source's own qualifier removed (``t.k`` and ``k`` are the same column)."""

    if isinstance(column, exp.Column) and column.table and column.table.lower() in source_names:
        return column.name.lower()
    return column.sql().lower()


def push_filter_through_windows(tree: exp.Expression) -> exp.Expression:
    """Apply the module's rewrite to every select of ``tree`` that qualifies. Returns the tree."""

    for select in list(tree.find_all(exp.Select)):
        _push(select)
    return tree


def _push(select: exp.Select) -> None:
    where = select.args.get("where")
    from_ = select.args.get("from_") or select.args.get("from")
    if where is None or from_ is None or select.args.get("joins") or select.args.get("laterals"):
        return
    source = from_.this
    if not isinstance(source, exp.Subquery) or not isinstance(source.this, exp.Select):
        return
    alias_node = source.args.get("alias")
    if alias_node is not None and alias_node.args.get("columns"):
        return
    inner = source.this
    if any(inner.args.get(k) for k in ("group", "having", "distinct", "order", "limit", "offset", "windows", "with_", "with")):
        return
    if _star(inner):
        return
    scoped = [inner.args.get(k) for k in ("where", "qualify")] + list(inner.expressions)
    if any(isinstance(n, (exp.Subquery, exp.Exists, exp.Select)) for part in scoped if part is not None for n in part.walk()):
        return
    windows = [w for w in inner.find_all(exp.Window) if w.find_ancestor(exp.Select) is inner]
    if not windows:
        return
    sources = _inner_source_names(inner)
    if sources is None:
        return
    partitions = []
    for window in windows:
        keys = window.args.get("partition_by")
        if not keys:
            return
        partitions.append({_key(k, sources) for k in keys})
    shared = set.intersection(*partitions)
    # output name -> the inner column it passes through, when it is a plain column that every window partitions by
    outputs: dict[str, exp.Expression | None] = {}
    for item in inner.expressions:
        name = (item.alias_or_name or "").lower()
        if not name:
            return
        value = item.this if isinstance(item, exp.Alias) else item
        outputs[name] = None if name in outputs else value
    movable = {
        name: value for name, value in outputs.items()
        if isinstance(value, exp.Column) and not isinstance(value.this, exp.Star) and _key(value, sources) in shared
    }
    # A partition column named in the select list by an alias would be read by the window as the source column,
    # never the alias (aliases are not visible in OVER), so only the plain-column value is compared above.
    alias = (source.alias or "").lower()
    moved: list[exp.Expression] = []
    kept: list[exp.Expression] = []
    for part in conjuncts(where.this):
        if _movable(part, alias, movable):
            moved.append(part)
        else:
            kept.append(part)
    if not moved:
        return
    pushed = []
    for part in moved:
        part = part.copy()
        for column in list(part.find_all(exp.Column)):
            replacement = movable[column.name.lower()].copy()
            if column is part:
                part = replacement
            else:
                column.replace(replacement)
        pushed.append(part)
    existing = [inner.args["where"].this.copy()] if inner.args.get("where") is not None else []
    inner.set("where", exp.Where(this=_and_all(existing + pushed)))
    select.set("where", exp.Where(this=_and_all([p.copy() for p in kept])) if kept else None)


def _movable(part: exp.Expression, alias: str, movable: dict[str, exp.Expression]) -> bool:
    if not _plain_shape(part):
        return False
    columns = list(part.find_all(exp.Column))
    if not columns:
        return False  # a constant filter: nothing to say about partitions
    return all((not c.table or (alias and c.table.lower() == alias)) and c.name.lower() in movable for c in columns)


def _inner_source_names(inner: exp.Select) -> set[str] | None:
    """The names the inner select's sources go by, or None when it has no plain FROM."""

    from_ = inner.args.get("from_") or inner.args.get("from")
    if from_ is None:
        return None
    names = set()
    sources = [from_.this] + [j.this for j in inner.args.get("joins") or []]
    for source in sources:
        if isinstance(source, (exp.Table, exp.Subquery)) and source.alias_or_name:
            names.add(source.alias_or_name.lower())
        else:
            return None
    return names if len(sources) == 1 else set()


def _and_all(parts: list[exp.Expression]) -> exp.Expression:
    result = parts[0]
    for part in parts[1:]:
        result = exp.And(this=result, expression=part)
    return result
