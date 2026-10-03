"""Drop window-function columns of a derived table that nothing reads.

``SELECT k, SUM(v) FROM (SELECT k, v, ROW_NUMBER() OVER (PARTITION BY k ORDER BY v) AS rn FROM t) AS d GROUP BY k``
is ``SELECT k, SUM(v) FROM (SELECT k, v FROM t) AS d GROUP BY k``: a window function adds a column and never
adds, removes or repeats a row, so a window column nobody reads can go. The prover keeps a select with a
window whole (``algebraic_equivalence._isolate_windows``), so without this the derived table on one side
would not match the window-free one on the other.

Kept as is when anything could read the column: a star (other than ``COUNT(*)``) or the table read as a
value anywhere in the enclosing select, a ``USING`` or ``NATURAL`` join, ``DISTINCT`` or a star in the derived
table, or any column of that name anywhere in the enclosing select (including the derived table's own
``QUALIFY`` and ``ORDER BY``), or a derived table that aggregates without a GROUP BY. At least one column always
stays.
"""

from __future__ import annotations

from sqlglot import exp


def _is_star(node: exp.Expression) -> bool:
    return isinstance(node, exp.Star) or (isinstance(node, exp.Column) and isinstance(node.this, exp.Star))


def _own_window(item: exp.Expression, select: exp.Select) -> bool:
    return any(w.find_ancestor(exp.Select) is select for w in item.find_all(exp.Window))


def _global_aggregate(select: exp.Select) -> bool:
    """``select`` aggregates without a GROUP BY (``SUM(COUNT(*)) OVER ()`` counts): it returns one row even over
    no input, and dropping the column holding its only aggregate would leave a row per input row."""

    return not select.args.get("group") and any(
        a.find_ancestor(exp.Select) is select and not isinstance(a.parent, exp.Window) for a in select.find_all(exp.AggFunc)
    )


def drop_unread_windows(tree: exp.Expression) -> exp.Expression:
    """Remove unread window columns from every derived table of ``tree`` (module doc). Rewrites in place."""

    for subquery in list(tree.find_all(exp.Subquery)):
        inner = subquery.this
        alias = (subquery.alias or "").lower()
        holder = subquery.parent
        outer = holder.parent if isinstance(holder, (exp.From, exp.Join)) else None
        if not alias or not isinstance(inner, exp.Select) or not isinstance(outer, exp.Select):
            continue
        if inner.args.get("distinct") or any(_is_star(e) for e in inner.expressions):
            continue
        if not any(_own_window(e, inner) for e in inner.expressions):
            continue
        if _global_aggregate(inner):
            continue
        if any(isinstance(s, exp.Star) and not isinstance(s.parent, exp.Count) for s in outer.find_all(exp.Star)):
            continue
        if any(j.args.get("using") or j.args.get("kind", "").upper() == "NATURAL" or j.args.get("method", "").upper() == "NATURAL"
               for j in outer.args.get("joins") or []):
            continue
        keep = []
        for item in inner.expressions:
            name = (item.alias_or_name or "").lower()
            if not name or not _own_window(item, inner):
                keep.append(item)
                continue
            mentioned = {
                c.name.lower() for c in outer.find_all(exp.Column)
                if c.find_ancestor(exp.Alias) is not item and c is not item
            }
            if name in mentioned or alias in mentioned:
                keep.append(item)
        if not keep:
            keep = inner.expressions[:1]
        if len(keep) < len(inner.expressions):
            inner.set("expressions", keep)
    return tree
