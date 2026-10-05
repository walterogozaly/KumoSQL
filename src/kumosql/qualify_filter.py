"""``QUALIFY`` of a grouped select, spelled as a filter over a derived table.

``SELECT k, COUNT(*) AS c FROM t GROUP BY k QUALIFY RANK() OVER (ORDER BY COUNT(*) DESC) = 1`` is
``SELECT d.k, d.c FROM (SELECT k, COUNT(*) AS c, RANK() OVER (ORDER BY COUNT(*) DESC) AS h0 FROM t GROUP BY k) AS d WHERE d.h0 = 1``.

QUALIFY runs after grouping, HAVING and the windows and before DISTINCT, ORDER BY and LIMIT. A derived table
that computes the same windows over the same FROM, WHERE, GROUP BY and HAVING, with the QUALIFY condition
as the filter of the select above it, returns the same rows in the same columns. The rewrite moves the window
text unchanged into a select with the same scope (same sources, same output aliases), so a window sees exactly
what it saw before; it holds on every database, ties included, because the windows are not touched.

The derived-table spelling is the normal form: the prover already reads a user-written
``SELECT .. FROM (SELECT .., window AS w FROM t) WHERE pred(w)`` through
``algebraic_equivalence._isolate_windows`` and ``window_canonical``, and the same code does the same for
an ungrouped select with QUALIFY. That is why this module does nothing for an ungrouped select: it is already
covered, and a second implementation could only disagree with it. A select that groups
(``GROUP BY``, ``HAVING`` or a plain aggregate) is the gap: ``_isolate_windows`` wraps it whole and refuses
a QUALIFY, so the QUALIFY spelling never read like the derived one.

Preconditions (anything else is left as written):

* the select groups (``GROUP BY``, ``HAVING`` or an aggregate outside a window) and has a ``QUALIFY``;
* no ``ORDER BY``, ``LIMIT``, ``OFFSET`` or ``WINDOW`` clause, no ``DISTINCT ON``, no ``*`` in the select list,
  and every output is an alias or a plain column (an unnamed expression has a name this rule cannot read back);
* the QUALIFY condition has no subquery and no aggregate outside a window, and every column it reads outside a
  window is either an output that is a plain column (read back by name), or a ``GROUP BY`` column (added as a
  hidden output). A bare name that is also the alias of another output is refused: whether BigQuery reads it as
  the alias or the source column is not something this rule tries to decide;
* a window already in the select list is read back from that output instead of being computed twice.

``DISTINCT`` moves to the outer select (it runs after QUALIFY).
"""

from __future__ import annotations

import itertools

from sqlglot import exp

from .ast_utils import FROM_KEY

_COUNTER = itertools.count()


def _star(select: exp.Select) -> bool:
    return any(isinstance(s, exp.Star) or isinstance(s, exp.Column) and isinstance(s.this, exp.Star) for s in select.expressions)


def _window_function(call: exp.Expression) -> bool:
    node = call
    while isinstance(node.parent, (exp.Filter, exp.IgnoreNulls, exp.RespectNulls)) and node.arg_key == "this":
        node = node.parent
    return isinstance(node.parent, exp.Window) and node.arg_key == "this"


def _groups(select: exp.Select) -> bool:
    """Whether ``select`` groups: a GROUP BY, a HAVING or an aggregate that is not a window's function."""

    if select.args.get("group") or select.args.get("having"):
        return True
    return any(
        a.find_ancestor(exp.Select) is select and not _window_function(a)
        for a in select.find_all(exp.AggFunc)
    )


def _group_texts(select: exp.Select) -> set[str]:
    group = select.args.get("group")
    if group is None or any(v for k, v in group.args.items() if k != "expressions"):
        return set()
    return {g.sql() for g in group.expressions}


def qualify_to_filter(tree: exp.Expression) -> exp.Expression:
    """Rewrite every grouped select of ``tree`` that has a QUALIFY (module doc). Returns the tree."""

    for select in list(tree.find_all(exp.Select))[::-1]:
        rewritten = _rewrite(select)
        if rewritten is None:
            continue
        if select is tree:
            tree = rewritten
        else:
            select.replace(rewritten)
    return tree


def _rewrite(select: exp.Select) -> exp.Select | None:
    qualify = select.args.get("qualify")
    if qualify is None or select.args.get("windows") or not _groups(select) or _star(select):
        return None
    if any(select.args.get(k) for k in ("order", "limit", "offset")):
        return None
    distinct = select.args.get("distinct")
    if distinct is not None and distinct.args.get("on"):
        return None
    condition = qualify.this
    if any(isinstance(n, (exp.Subquery, exp.Exists, exp.Select)) for n in condition.walk()):
        return None
    items = select.expressions
    if any(not isinstance(i, (exp.Alias, exp.Column)) for i in items):
        return None  # an unnamed output: BigQuery names it, this rule cannot read it back
    names = [i.alias_or_name for i in items]
    if any(not n for n in names) or len({n.lower() for n in names}) != len(names):
        return None
    values = [i.this if isinstance(i, exp.Alias) else i for i in items]
    # outputs readable by their value: plain columns, and a window that is the whole output
    by_column: dict[str, str] = {}
    by_window: dict[str, str] = {}
    for name, value in zip(names, values):
        if isinstance(value, exp.Column):
            by_column.setdefault(value.sql(), name)
        elif isinstance(value, exp.Window):
            by_window.setdefault(value.sql(), name)
    aliases = {n.lower() for n, v in zip(names, values) if not (isinstance(v, exp.Column) and v.name.lower() == n.lower())}
    groups = _group_texts(select)
    alias = f"kqf{next(_COUNTER)}"
    hidden: list[exp.Expression] = []
    hidden_names: dict[str, str] = {}
    used = {n.lower() for n in names}

    def hide(key: str, value: exp.Expression) -> str:
        if key not in hidden_names:
            name = f"kqh{len(hidden)}"
            while name in used:
                name += "x"
            used.add(name)
            hidden_names[key] = name
            hidden.append(exp.alias_(value.copy(), name))
        return hidden_names[key]

    failed = False

    def swap(node: exp.Expression) -> exp.Expression:
        nonlocal failed
        if isinstance(node, exp.Window):
            text = node.sql()
            return exp.column(by_window[text] if text in by_window else hide("w:" + text, node), table=alias)
        if isinstance(node, exp.Column):
            text = node.sql()
            if text in by_column:
                return exp.column(by_column[text], table=alias)
            if not node.table and node.name.lower() in aliases:
                failed = True  # an alias or a source column: not decided here
            elif text in groups:
                return exp.column(hide("c:" + text, node), table=alias)
            else:
                failed = True
            return node
        if isinstance(node, exp.AggFunc):
            failed = True
        return node

    def transform(node: exp.Expression) -> exp.Expression:
        new = swap(node)
        if new is not node:
            return new
        for child in list(node.iter_expressions()):
            replaced = transform(child)
            if replaced is not child:
                child.replace(replaced)
        return node

    filtered = transform(condition.copy())
    if failed:
        return None
    inner = select.copy()
    inner.set("qualify", None)
    inner.set("distinct", None)
    inner.set("expressions", [i.copy() for i in items] + hidden)
    outer = exp.Select(expressions=[exp.alias_(exp.column(n, table=alias), n) for n in names])
    outer.set(FROM_KEY, exp.From(this=exp.Subquery(this=inner, alias=exp.TableAlias(this=exp.to_identifier(alias)))))
    outer.set("where", exp.Where(this=filtered))
    if distinct is not None:
        outer.set("distinct", distinct.copy())
    return outer
