"""One spelling for windows that compute the same thing, so the prover's text comparison of them matches.

The prover keeps window computations whole and compares them by their text (``_isolate_windows``). These
rewrites, run just before that, give equal windows equal text. Each one keeps every row and every value.

* ``canonical_frame``: on ``SUM``, ``COUNT``, ``MIN``, ``MAX``, ``AVG``, ``COUNTIF``, ``LOGICAL_AND``,
  ``LOGICAL_OR`` and ``BIT_AND``/``BIT_OR``/``BIT_XOR`` (whose value depends only on the set of rows in the
  frame, not their order):

  - a literal ``PARTITION BY`` key, or one repeated, splits nothing and is dropped;
  - an ``ORDER BY`` key that is a literal, a ``PARTITION BY`` key or an earlier ``ORDER BY`` key is equal on
    every row of a partition, so it never orders two rows or tells peers apart; under a ``RANGE`` frame (or
    the default one) it is dropped, and with no key left every row is a peer, so the frame is the partition;
  - the explicit default frame is dropped: ``RANGE BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW`` with an
    ``ORDER BY``; without one, any ``RANGE`` frame bounded by ``UNBOUNDED`` or ``CURRENT ROW`` (all rows are
    peers) is the whole partition, the default;
  - a frame ``BETWEEN UNBOUNDED PRECEDING AND UNBOUNDED FOLLOWING`` (``ROWS`` or ``RANGE``) is the whole
    partition, so its ``ORDER BY`` cannot matter to these functions and goes with it.

  ``ROWS`` frames bounded by ``CURRENT ROW`` are never touched: they depend on the order among peers.
  ``FIRST_VALUE``, ``LAST_VALUE`` and ``NTH_VALUE`` only lose the explicit default frame next to an
  ``ORDER BY`` (the same frame spelled out), and, with no ``ORDER BY``, a ``RANGE`` frame bounded by
  ``UNBOUNDED`` or ``CURRENT ROW`` or a ``ROWS`` frame from ``UNBOUNDED PRECEDING`` to ``UNBOUNDED FOLLOWING``
  (every row is a peer, so each is the whole partition, the default). ``unique_order_frames`` reads a ``ROWS``
  frame as ``RANGE`` before this runs when the order keys cannot tie.
* ``WHERE c = 5`` on an integer column ``c`` of the select's only table makes ``c`` the literal 5 on every
  row its windows see, so those windows read 5 for ``c`` (then the rules above drop it as a key).
* ``merge_grouped_source``: windows over ``(SELECT k, agg AS a .. GROUP BY k) AS d`` read the grouped rows;
  written over the ``GROUP BY`` itself (``agg`` for ``d.a``) they read the same rows, since a grouped
  select computes its windows after grouping and ``HAVING``.
* ``isolate_grouped_windows``: a grouped select with windows becomes a plain select over a derived table
  ``kqw*`` that computes, per group, its group keys, aggregates and windows (named by their sorted text),
  which the prover keeps whole like ``_isolate_windows`` does for an ungrouped select.
* ``prune_unread_windows``: a derived table's window column that the query around it never reads is
  dropped; a window adds a value to each row and never adds, removes or repeats one. Not when that
  column holds the derived table's only aggregate outside a window (the table would stop being one row).
"""

from __future__ import annotations

import itertools

from sqlglot import exp

from .ast_utils import FROM_KEY

_ORDER_BLIND = (exp.Sum, exp.Count, exp.Min, exp.Max, exp.Avg) + tuple(
    getattr(exp, name)
    for name in ("CountIf", "LogicalAnd", "LogicalOr", "BitwiseAndAgg", "BitwiseOrAgg", "BitwiseXorAgg")
    if hasattr(exp, name)
)
_NAVIGATION = (exp.FirstValue, exp.LastValue, exp.NthValue)
_INTEGER_TYPES = {"INT", "INTEGER", "BIGINT", "SMALLINT", "TINYINT", "MEDIUMINT", "INT64", "INT32", "INT16", "INT8", "INT4", "INT2"}
_COUNTER = itertools.count()


def canonical_windows(tree: exp.Expression, types: dict | None = None) -> exp.Expression:
    """Apply the module's rewrites to every select of ``tree``, innermost first (module doc)."""

    lowered = {str(t).lower(): {str(c).lower(): str(v) for c, v in cols.items()} for t, cols in (types or {}).items()}
    for select in list(tree.find_all(exp.Select))[::-1]:
        if not _owned_windows(select) or select.args.get("windows"):
            continue
        merged = merge_grouped_source(select)
        if merged is not None:
            if select is tree:
                tree = merged
            else:
                select.replace(merged)
            select = merged
        constants = _where_constants(select, lowered)
        for window in _owned_windows(select):
            canonical_frame(window, constants)
    for select in list(tree.find_all(exp.Select))[::-1]:
        pruned = prune_unread_windows(select)
        if pruned is not None:
            if select is tree:
                tree = pruned
            else:
                select.replace(pruned)
    for select in list(tree.find_all(exp.Select))[::-1]:
        isolated = isolate_grouped_windows(select)
        if isolated is not None:
            if select is tree:
                tree = isolated
            else:
                select.replace(isolated)
    return tree


def _owned_windows(select: exp.Select) -> list[exp.Window]:
    return [w for w in select.find_all(exp.Window) if w.find_ancestor(exp.Select) is select]


def _is_constant(node: exp.Expression) -> bool:
    return isinstance(node, (exp.Literal, exp.Null, exp.Boolean))


def _bound(spec: exp.WindowSpec) -> tuple | None:
    """``(kind, start, end)`` of a frame bounded by UNBOUNDED or CURRENT ROW only; None otherwise."""

    if spec.args.get("exclude"):
        return None
    kind = str(spec.args.get("kind") or "").upper()
    start = (str(spec.args.get("start") or "").upper(), str(spec.args.get("start_side") or "").upper())
    end = (str(spec.args.get("end") or "CURRENT ROW").upper(), str(spec.args.get("end_side") or "").upper())
    named = {("UNBOUNDED", "PRECEDING"): "UP", ("CURRENT ROW", ""): "CR", ("UNBOUNDED", "FOLLOWING"): "UF"}
    if kind not in ("ROWS", "RANGE") or named.get(start) not in ("UP", "CR") or named.get(end) not in ("CR", "UF"):
        return None
    return kind, named[start], named[end]


def canonical_frame(window: exp.Window, constants: dict | None = None) -> bool:
    """Rewrite ``window`` in place into its canonical spelling (module doc); whether it changed."""

    if window.args.get("alias") or window.args.get("first") or any(window.find_all(exp.Subquery, exp.Exists)):
        return False
    before = window.sql()
    function = window.this
    if constants:
        for column in list(window.find_all(exp.Column)):
            key = (column.table.lower(), column.name.lower())
            value = constants.get(key)
            if value is not None:
                column.replace(value.copy())
    if isinstance(function, _NAVIGATION) and window.args.get("spec") is not None:
        shape = _bound(window.args["spec"])
        if window.args.get("order"):
            if shape == ("RANGE", "UP", "CR"):
                window.set("spec", None)
        elif shape is not None and (shape[0] == "RANGE" or shape[1:] == ("UP", "UF")):
            window.set("spec", None)  # no ORDER BY: every row is a peer, so these frames are the whole partition
        return window.sql() != before
    if not isinstance(function, _ORDER_BLIND):
        return window.sql() != before
    spec = window.args.get("spec")
    shape = _bound(spec) if spec is not None else None
    if spec is not None and shape is None:
        return window.sql() != before  # an offset frame or EXCLUDE: left alone
    partitions, seen = [], set()
    for key in window.args.get("partition_by") or []:
        text = key.sql()
        if _is_constant(key) or text in seen:
            continue
        seen.add(text)
        partitions.append(key)
    window.set("partition_by", partitions or None)
    order = window.args.get("order")
    if order is not None and (spec is None or shape[0] == "RANGE"):
        kept = []
        for ordered in order.expressions:
            value = ordered.this if isinstance(ordered, exp.Ordered) else ordered
            text = value.sql()
            if _is_constant(value) or text in seen:
                continue
            seen.add(text)
            kept.append(ordered)
        if kept:
            order.set("expressions", kept)
        else:
            window.set("order", None)
            window.set("spec", None)  # every row is a peer: any such RANGE frame is the whole partition
    order = window.args.get("order")
    spec = window.args.get("spec")
    if spec is not None:
        kind, start, end = _bound(spec)
        if start == "UP" and end == "UF":
            window.set("spec", None)
            window.set("order", None)
        elif kind == "RANGE" and (order is None or (start, end) == ("UP", "CR")):
            window.set("spec", None)
    return window.sql() != before


def _where_constants(select: exp.Select, types: dict) -> dict:
    """``{(qualifier, column): literal}`` for each ``c = <integer>`` conjunct of WHERE on an integer column."""

    where = select.args.get("where")
    from_ = select.args.get("from_") or select.args.get("from")
    if where is None or from_ is None or select.args.get("joins") or select.args.get("group") or select.args.get("having"):
        return {}
    table = from_.this
    if not isinstance(table, exp.Table):
        return {}
    columns = types.get(table.name.lower())
    if not columns:
        return {}
    alias = table.alias_or_name.lower()
    found: dict = {}
    for conjunct in where.this.flatten() if isinstance(where.this, exp.And) else [where.this]:
        if not isinstance(conjunct, exp.EQ):
            continue
        left, right = conjunct.this, conjunct.expression
        if isinstance(left, exp.Literal):
            left, right = right, left
        if not isinstance(left, exp.Column) or not isinstance(right, exp.Literal) or right.is_string:
            continue
        if not right.this.isdigit() or left.table and left.table.lower() != alias:
            continue
        kind = columns.get(left.name.lower(), "").upper().split("(")[0].strip()
        if kind not in _INTEGER_TYPES:
            continue
        for key in (("", left.name.lower()), (alias, left.name.lower())):
            if key in found and found[key].sql() != right.sql():
                return {}  # contradictory: leave it to the rest of the prover
            found[key] = right
    return found


def _group_texts(select: exp.Select) -> set[str] | None:
    group = select.args.get("group")
    if group is None or not group.expressions or any(v for k, v in group.args.items() if k != "expressions"):
        return None
    return {g.sql() for g in group.expressions}


def _own_aggregates(select: exp.Select, node: exp.Expression) -> list[exp.AggFunc]:
    """Aggregate calls of ``select`` under ``node`` that are not a window's own function or inside one."""

    return [a for a in node.find_all(exp.AggFunc) if a.find_ancestor(exp.Select) is select and not _window_function(a)]


def _window_function(call: exp.Expression) -> bool:
    """Whether ``call`` is the function a window computes (``SUM`` of ``SUM(x) OVER (..)``)."""

    node = call
    while isinstance(node.parent, (exp.Filter, exp.IgnoreNulls, exp.RespectNulls)) and node.arg_key == "this":
        node = node.parent
    return isinstance(node.parent, exp.Window) and node.arg_key == "this"


def _alias_clash(select: exp.Select, parts: list[exp.Expression]) -> bool:
    """Whether a bare column in ``parts`` shares its name with an output alias that is not that column."""

    aliases = {
        item.alias.lower() for item in select.expressions
        if isinstance(item, exp.Alias) and not (isinstance(item.this, exp.Column) and item.this.name.lower() == item.alias.lower())
    }
    return any(not c.table and c.name.lower() in aliases for part in parts if part is not None for c in part.find_all(exp.Column))


def _covered(select: exp.Select, value: exp.Expression, groups: set[str]) -> bool:
    """Whether every column of ``value`` outside an aggregate and a window sits in a GROUP BY expression."""

    def walk(node: exp.Expression) -> bool:
        if node.sql() in groups:
            return True
        if isinstance(node, (exp.AggFunc, exp.Window)):
            return True
        if isinstance(node, exp.Column):
            return False
        return all(walk(child) for child in node.iter_expressions())

    return walk(value)


def _star(select: exp.Select) -> bool:
    return any(isinstance(s, exp.Star) or isinstance(s, exp.Column) and isinstance(s.this, exp.Star) for s in select.expressions)


def merge_grouped_source(select: exp.Select) -> exp.Select | None:
    """Windows over a grouped derived table, read over the grouping itself (module doc)."""

    if any(select.args.get(k) for k in ("joins", "where", "group", "having", "qualify", "distinct", "order", "limit", "offset", "windows")):
        return None
    from_ = select.args.get("from_") or select.args.get("from")
    source = from_.this if from_ is not None else None
    if not isinstance(source, exp.Subquery) or not source.alias or not isinstance(source.this, exp.Select) or _star(select):
        return None
    inner = source.this
    groups = _group_texts(inner)
    if groups is None or _star(inner) or any(inner.args.get(k) for k in ("qualify", "distinct", "order", "limit", "offset", "windows", "with_", "with")):
        return None
    if any(inner.find_all(exp.Window)) or any(
        n is not select and not _inside(n, source) for n in select.find_all(exp.Subquery, exp.Exists, exp.Select)
    ):
        return None
    if any(isinstance(n, (exp.Subquery, exp.Exists)) for item in inner.expressions for n in item.walk()):
        return None
    if any(isinstance(n, (exp.Subquery, exp.Exists)) for n in (inner.args.get("having") or exp.Null()).walk()):
        return None
    if any(not _window_function(a) for a in select.find_all(exp.AggFunc) if not _inside(a, source)):
        return None  # an aggregate of the outer select itself, not a window function
    outputs: dict[str, exp.Expression] = {}
    for item in inner.expressions:
        name = item.alias_or_name.lower()
        if not name or name in outputs:
            return None
        value = item.this if isinstance(item, exp.Alias) else item
        if not _covered(inner, value, groups):
            return None
        outputs[name] = value
    if _alias_clash(inner, [inner.args.get("group"), inner.args.get("having")]):
        return None
    alias = source.alias.lower()
    if alias in outputs:
        return None  # ``d`` could be the column or the whole row
    result = select.copy()
    result_source = (result.args.get("from_") or result.args.get("from")).this
    for column in list(result.find_all(exp.Column)):
        if _inside(column, result_source):
            continue
        if column.table and column.table.lower() != alias or column.name.lower() not in outputs:
            return None
        replacement = outputs[column.name.lower()].copy()
        top = column.parent is result
        if isinstance(replacement, (exp.Binary, exp.Connector)) and not isinstance(column.parent, (exp.Func, exp.Alias, exp.Paren, exp.Ordered, exp.Window, exp.Tuple)):
            replacement = exp.Paren(this=replacement)
        if top and not (isinstance(replacement, exp.Column) and replacement.name.lower() == column.name.lower()):
            replacement = exp.alias_(replacement, column.name)
        column.replace(replacement)
    result.set(FROM_KEY, (inner.args.get("from_") or inner.args.get("from")).copy())
    for key in ("joins", "where", "group", "having"):
        value = inner.args.get(key)
        result.set(key, [j.copy() for j in value] if isinstance(value, list) else value.copy() if value is not None else None)
    if _alias_clash(result, [result.args.get("group"), result.args.get("having")] + _owned_windows(result)):
        return None
    return result


def isolate_grouped_windows(select: exp.Select) -> exp.Select | None:
    """A grouped select's windows, aggregates and keys computed in a derived table ``kqw*`` (module doc)."""

    windows = _owned_windows(select)
    groups = _group_texts(select)
    if not windows or groups is None or _star(select):
        return None
    if any(select.args.get(k) for k in ("qualify", "distinct", "order", "limit", "offset", "windows")):
        return None
    parent = select.parent
    if isinstance(parent, exp.Subquery) and (parent.alias or "").startswith("kqw"):
        return None
    if not isinstance(parent, (exp.Subquery, exp.CTE, exp.From, exp.Join, type(None))):
        return None
    if any(isinstance(n, (exp.Subquery, exp.Exists, exp.Select)) for item in select.expressions for n in item.walk()):
        return None
    if _alias_clash(select, [select.args.get("group"), select.args.get("having")] + windows):
        return None
    atoms: dict[str, exp.Expression] = {}
    calls: dict[str, exp.Window] = {}

    def collect(node: exp.Expression) -> bool:
        if isinstance(node, exp.Window):
            calls.setdefault(node.sql(), node)
            return True
        if node.sql() in groups or isinstance(node, exp.AggFunc):
            atoms.setdefault(node.sql(), node)
            return True
        if isinstance(node, exp.Column):
            return False
        return all(collect(child) for child in node.iter_expressions())

    for item in select.expressions:
        if not collect(item.this if isinstance(item, exp.Alias) else item):
            return None
    alias = f"kqwg{next(_COUNTER)}"
    atom_names = {sql: f"kqc{i}" for i, sql in enumerate(sorted(atoms))}
    window_names = {sql: f"kqv{i}" for i, sql in enumerate(sorted(calls))}

    def swap(node: exp.Expression) -> exp.Expression:
        text = node.sql()
        if isinstance(node, exp.Window) and text in window_names:
            return exp.column(window_names[text], table=alias)
        if text in atom_names:
            return exp.column(atom_names[text], table=alias)
        return node

    inner = exp.Select(
        expressions=[exp.alias_(atoms[sql].copy(), name) for sql, name in sorted(atom_names.items())]
        + [exp.alias_(calls[sql].copy(), name) for sql, name in sorted(window_names.items())]
    )
    inner.set(FROM_KEY, (select.args.get("from_") or select.args.get("from")).copy())
    for key in ("joins", "where", "group", "having"):
        value = select.args.get(key)
        if value is not None:
            inner.set(key, [j.copy() for j in value] if isinstance(value, list) else value.copy())
    items = []
    for item in select.expressions:
        value = item.this if isinstance(item, exp.Alias) else item
        new = _swap_top_down(value.copy(), swap)
        if isinstance(item, exp.Alias):
            new = exp.alias_(new, item.alias)
        elif isinstance(item, exp.Column):
            new = exp.alias_(new, item.name)
        items.append(new)
    outer = exp.Select(expressions=items)
    outer.set(FROM_KEY, exp.From(this=exp.Subquery(this=inner, alias=exp.TableAlias(this=exp.to_identifier(alias)))))
    return outer


def _swap_top_down(node: exp.Expression, swap) -> exp.Expression:
    new = swap(node)
    if new is not node:
        return new
    for child in list(node.iter_expressions()):
        replaced = _swap_top_down(child, swap)
        if replaced is not child:
            child.replace(replaced)
    return node


def _is_aggregate(select: exp.Select) -> bool:
    return bool(select.args.get("group") or select.args.get("having") or any(_own_aggregates(select, i) for i in select.expressions))


def prune_unread_windows(select: exp.Select) -> exp.Select | None:
    """Drop window columns of ``select``'s derived source that ``select`` never reads (module doc)."""

    from_ = select.args.get("from_") or select.args.get("from")
    source = from_.this if from_ is not None else None
    if not isinstance(source, exp.Subquery) or not isinstance(source.this, exp.Select) or _star(select):
        return None
    inner = source.this
    if any(inner.args.get(k) for k in ("distinct", "order", "limit", "offset", "qualify", "windows")) or _star(inner):
        return None
    if any(j.args.get("using") or j.args.get("method") for j in select.args.get("joins") or []):
        return None  # USING and NATURAL read columns by name without a column reference
    reads = {c.name.lower() for c in select.find_all(exp.Column) if not _inside(c, source)}
    if (source.alias or "").lower() in reads or any(isinstance(n, exp.Star) for n in select.walk() if not _inside(n, source)):
        return None  # the whole row read as a value, or every column
    own = {c.name.lower() for part in (inner.args.get("group"), inner.args.get("having")) if part is not None for c in part.find_all(exp.Column)}
    names = [item.alias_or_name.lower() for item in inner.expressions]
    drop = [
        i for i, item in enumerate(inner.expressions)
        if names[i] and names.count(names[i]) == 1 and names[i] not in reads and names[i] not in own
        and any(w.find_ancestor(exp.Select) is inner for w in item.find_all(exp.Window))
        and not any(isinstance(n, (exp.Subquery, exp.Exists, exp.Select)) for n in item.walk())
    ]
    if not drop or len(drop) == len(inner.expressions):
        return None
    result = select.copy()
    result_inner = (result.args.get("from_") or result.args.get("from")).this.this
    kept = [item for i, item in enumerate(result_inner.expressions) if i not in drop]
    aggregate = _is_aggregate(result_inner)
    result_inner.set("expressions", kept)
    if aggregate != _is_aggregate(result_inner):
        return None  # the dropped column held the only aggregate of a global aggregate
    return result


def _inside(node: exp.Expression, ancestor: exp.Expression) -> bool:
    while node is not None:
        if node is ancestor:
            return True
        node = node.parent
    return False
