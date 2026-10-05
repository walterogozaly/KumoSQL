"""``LAG`` and ``LEAD`` spelled as a self-join on ``ROW_NUMBER`` ordinals.

``SELECT id, LAG(v) OVER (PARTITION BY k ORDER BY id) AS prev FROM t`` is the value of ``v`` one row earlier in
the partition's order. The same numbers come from numbering the rows and joining each row to the one numbered
one less:

    SELECT a.id, b.v AS prev
    FROM (SELECT id, k, ROW_NUMBER() OVER (PARTITION BY k ORDER BY id) AS n FROM t) AS a
    LEFT JOIN (SELECT v, k, ROW_NUMBER() OVER (PARTITION BY k ORDER BY id) AS n FROM t) AS b
      ON a.k IS NOT DISTINCT FROM b.k AND b.n = a.n - 1

``lag_lead_joins`` rewrites the first into the second (the form a person writes by hand) when all of these
hold, and otherwise leaves the select alone:

* the order is total: the select reads one plain table, and the window's ``PARTITION BY`` and ``ORDER BY``
  plain columns include a declared key whose columns are all NOT NULL (``window_order_keys``). Without it,
  rows that tie on the order can swap places, ``LAG`` and the two ``ROW_NUMBER`` numberings may each break the
  tie its own way, and the join would pair a row with a neighbour the window never gave it. With ties the
  rewrite does not fire;
* every window of the select is ``LAG`` or ``LEAD`` (not ``IGNORE NULLS``) with the same ``PARTITION BY`` and
  ``ORDER BY``, a literal offset of at least 1 (``LAG(v, 0)`` is the row itself and is left alone; a
  parameter or column offset is never rewritten) and a default that is a constant (a literal, signed
  number or NULL) or omitted. The value is a plain expression of the row (no aggregate, window or
  subquery);
* the select has no ``GROUP BY``, ``HAVING``, ``QUALIFY``, named ``WINDOW``, ``DISTINCT ON``, star or unnamed
  window output, and its ``WHERE`` and source are deterministic and free of subqueries, and no
  ``PARTITION BY`` column is a known floating-point column (see ``window_aggregate_joins``).

The numbering follows the window's own order and partition, so it is the order ``LAG`` reads (``DESC`` and NULL
placement included). The join is a ``LEFT JOIN``: the first row of a partition has no earlier row, and the
window then returns the default (NULL when none is given). A row that does have a neighbour whose value is NULL
returns that NULL, not the default, so the result is ``CASE WHEN b.n IS NULL THEN default ELSE b.v END`` and not a
``COALESCE``. ``LEAD`` joins at ``a.n + offset``. A NULL partition key is one partition, so partitions pair
null-safe (``=`` only when every key is a plain NOT NULL column). The ``WHERE`` runs inside both numberings,
since a window sees the rows it leaves.

The rewrite runs only as a later attempt of the prover (``window_joins``), after the plain attempt fails.
"""

from __future__ import annotations

from sqlglot import exp

from .ast_utils import FROM_KEY
from .window_aggregate_joins import WINDOW_JOIN_ASSUMPTION, _UNSUPPORTED_CLAUSES, _known_float, _never_null, _owned_windows, _plain, deterministic, source_of
from .window_order_keys import covers_a_key


def lag_lead_joins(
    tree: exp.Expression,
    keys: dict[str, list[tuple[str, ...]]] | None,
    not_null: dict[str, frozenset[str]] | None,
    types: dict[str, dict[str, str]] | None = None,
    assumptions: set[str] | None = None,
) -> exp.Expression:
    """Rewrite each eligible select of ``tree`` (module doc); returns the (possibly new) root."""

    for join in list(tree.find_all(exp.Join)):
        _orient_ordinal_condition(join)
    for select in list(tree.find_all(exp.Select))[::-1]:
        rewritten = _rewrite(select, keys or {}, not_null or {}, types or {})
        if rewritten is not None:
            if assumptions is not None:
                assumptions.add(WINDOW_JOIN_ASSUMPTION)
            if select is tree:
                tree = rewritten
            else:
                select.replace(rewritten)
    return tree


def _ordinal_column(join: exp.Join, column: exp.Expression) -> bool:
    """Whether ``column`` is ``alias.name`` of a derived table among the select's sources whose ``name`` is a ``ROW_NUMBER()``."""

    if not isinstance(column, exp.Column) or not column.table:
        return False
    select = join.parent
    sources = [select.args.get(FROM_KEY).this if select.args.get(FROM_KEY) is not None else None, *(j.this for j in select.args.get("joins") or [])]
    for source in sources:
        if isinstance(source, exp.Subquery) and source.alias.lower() == column.table.lower() and isinstance(source.this, exp.Select):
            items = [i for i in source.this.expressions if i.alias_or_name.lower() == column.name.lower()]
            return len(items) == 1 and isinstance(items[0], exp.Alias) and isinstance(items[0].this, exp.Window) and isinstance(items[0].this.this, exp.RowNumber)
    return False


def _shifted(node: exp.Expression) -> tuple[exp.Column, int] | None:
    """``(column, k)`` when ``node`` is ``column + k`` or ``column - k`` (``k`` an integer literal), with ``k`` signed."""

    if isinstance(node, (exp.Add, exp.Sub)) and isinstance(node.this, exp.Column):
        step = node.expression
        if isinstance(step, exp.Literal) and not step.is_string and step.this.isdigit():
            return node.this, int(step.this) * (1 if isinstance(node, exp.Add) else -1)
    return None


def _orient_ordinal_condition(join: exp.Join) -> None:
    """``ON a.n = b.n + 1`` (and its mirrored spellings) as ``ON b.n = a.n - 1``: the joined source's ordinal alone on the left.

    Only between two ``ROW_NUMBER()`` columns of different derived tables: integers, so ``x = y + k`` and ``y = x - k``
    say the same. It makes the neighbour condition of a hand-written ordinal self-join read like the one
    ``lag_lead_joins`` writes.
    """

    on, target = join.args.get("on"), join.this
    if on is None or not isinstance(target, exp.Subquery) or not target.alias or not isinstance(join.parent, exp.Select):
        return
    parts = list(on.flatten()) if isinstance(on, exp.And) else [on]
    changed = False
    for index, part in enumerate(parts):
        if not isinstance(part, exp.EQ):
            continue
        new = None
        for bare, moved in ((part.left, part.right), (part.right, part.left)):
            shift = _shifted(moved)
            if shift is None or not isinstance(bare, exp.Column):
                continue
            base, k = shift
            if not (_ordinal_column(join, bare) and _ordinal_column(join, base)) or bare.table.lower() == base.table.lower():
                continue
            # bare = base + k
            if base.table.lower() == target.alias.lower():
                # base + k = bare  ->  base = bare - k   (the target's column alone on the left)
                other, delta, own = bare, -k, base
            elif bare.table.lower() == target.alias.lower():
                other, delta, own = base, k, bare
            else:
                continue
            offset = exp.Literal.number(abs(delta))
            right = exp.Add(this=other.copy(), expression=offset) if delta > 0 else exp.Sub(this=other.copy(), expression=offset)
            new = exp.EQ(this=own.copy(), expression=right) if delta != 0 else exp.EQ(this=own.copy(), expression=other.copy())
            break
        if new is not None and new.sql() != part.sql():
            parts[index] = new
            changed = True
    if changed:
        condition = parts[0]
        for part in parts[1:]:
            condition = exp.And(this=condition, expression=part)
        join.set("on", condition)


def _constant(node: exp.Expression | None) -> bool:
    if node is None or isinstance(node, (exp.Null, exp.Boolean)):
        return True
    if isinstance(node, exp.Neg):
        node = node.this
        return isinstance(node, exp.Literal) and not node.is_string
    return isinstance(node, exp.Literal)


def _offset(function: exp.Expression) -> int | None:
    offset = function.args.get("offset")
    if offset is None:
        return 1
    if isinstance(offset, exp.Literal) and not offset.is_string and offset.this.isdigit() and int(offset.this) >= 1:
        return int(offset.this)
    return None


def _eligible(window: exp.Window) -> bool:
    function = window.this
    if not isinstance(function, (exp.Lag, exp.Lead)) or window.args.get("alias") or window.args.get("first"):
        return False
    if window.args.get("spec") is not None or window.args.get("order") is None:
        return False
    if _offset(function) is None or not _constant(function.args.get("default")):
        return False
    return function.this is not None and _plain(function.this) and all(_plain(k) for k in window.args.get("partition_by") or [])


def _signature(window: exp.Window) -> tuple:
    partition = tuple(k.sql() for k in window.args.get("partition_by") or [])
    return partition, window.args["order"].sql()


def _rewrite(select: exp.Select, keys: dict, not_null: dict, types: dict) -> exp.Select | None:
    windows = _owned_windows(select)
    source = source_of(select)
    if not windows or not isinstance(source, exp.Table) or any(select.args.get(k) for k in _UNSUPPORTED_CLAUSES):
        return None
    if select.args.get("distinct") is not None and select.args["distinct"].args.get("on"):
        return None
    if not all(_eligible(w) for w in windows) or len({_signature(w) for w in windows}) != 1:
        return None
    where = select.args.get("where")
    if where is not None and not deterministic(where):
        return None
    first = windows[0]
    partition = list(first.args.get("partition_by") or [])
    order = first.args["order"]
    if not covers_a_key(select, partition, order.expressions, keys, not_null):
        return None
    if any(_known_float(source, key, types) for key in partition):
        return None
    for item in select.expressions:
        if isinstance(item, exp.Window) or any(isinstance(n, exp.Star) for n in item.walk() if n.find_ancestor(exp.Window) is None):
            return None
    names = (source.name.lower(), source.alias_or_name.lower())
    aliases = {i.alias.lower() for i in select.expressions if isinstance(i, exp.Alias)}

    def ours(column: exp.Column) -> bool:
        return not isinstance(column.this, exp.Star) and (not column.table or column.table.lower() in names)

    outside = [c for part in [*select.expressions, select.args.get("order")] if part is not None for c in part.find_all(exp.Column)
               if c.find_ancestor(exp.Window) is None]
    if any(not ours(c) for c in outside if c.table):
        return None
    read = sorted({c.name.lower() for c in outside if c.table or c.name.lower() not in aliases})
    window_columns = [c for w in windows for c in w.find_all(exp.Column)]
    if any(not ours(c) for c in window_columns):
        return None
    null_safe = not _never_null(source, partition, not_null)

    def numbered(items: list[exp.Expression]) -> exp.Select:
        number = exp.Window(
            this=exp.RowNumber(),
            partition_by=[k.copy() for k in partition] or None,
            order=order.copy(),
            over="OVER",
        )
        inner = exp.Select(expressions=[*items, exp.alias_(number, "kqn")])
        inner.set(FROM_KEY, select.args[FROM_KEY].copy())
        if where is not None:
            inner.set("where", where.copy())
        return inner

    def keyed(items: list[exp.Expression]) -> list[exp.Expression]:
        return [*items, *(exp.alias_(k.copy(), f"kqp{n}") for n, k in enumerate(partition))]

    left_items = keyed([exp.column(name) for name in read])
    left = exp.Subquery(this=numbered(left_items), alias=exp.TableAlias(this=exp.to_identifier("kqa")))
    joins, swaps = [], {}
    for index, sql in enumerate(sorted({w.sql() for w in windows})):
        window = next(w for w in windows if w.sql() == sql)
        function = window.this
        n = _offset(function)
        alias = f"kqb{index}"
        right_items = keyed([exp.alias_(function.this.copy(), "kqv")])
        right = exp.Subquery(this=numbered(right_items), alias=exp.TableAlias(this=exp.to_identifier(alias)))
        a_n, b_n = exp.column("kqn", table="kqa"), exp.column("kqn", table=alias)
        shifted = exp.Sub(this=a_n, expression=exp.Literal.number(n)) if isinstance(function, exp.Lag) else exp.Add(this=a_n, expression=exp.Literal.number(n))
        condition = exp.EQ(this=b_n, expression=shifted)
        for p in range(len(partition)):
            pa, pb = exp.column(f"kqp{p}", table="kqa"), exp.column(f"kqp{p}", table=alias)
            condition = exp.And(this=exp.NullSafeEQ(this=pa, expression=pb) if null_safe else exp.EQ(this=pa, expression=pb), expression=condition)
        joins.append(exp.Join(this=right, on=condition, side="LEFT"))
        value = exp.column("kqv", table=alias)
        default = function.args.get("default")
        if default is not None and not isinstance(default, exp.Null):
            value = exp.Case(ifs=[exp.If(this=exp.Is(this=exp.column("kqn", table=alias), expression=exp.Null()), true=default.copy())], default=value)
        swaps[sql] = value

    def swap(node: exp.Expression) -> exp.Expression:
        if isinstance(node, exp.Window):
            return swaps[node.sql()].copy()
        if isinstance(node, exp.Column) and node.find_ancestor(exp.Window) is None and (node.table or node.name.lower() not in aliases):
            return exp.column(node.name.lower(), table="kqa")
        return node

    outer = select.copy()
    outer.set("expressions", [item.transform(swap) for item in outer.expressions])
    if outer.args.get("order") is not None:
        outer.set("order", outer.args["order"].transform(swap))
    outer.set("where", None)
    outer.set(FROM_KEY, exp.From(this=left))
    outer.set("joins", joins)
    return outer
