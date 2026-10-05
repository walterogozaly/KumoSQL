"""Drop the unread columns of a derived table that holds windows, and renumber what is left.

The prover reads a derived table that computes windows (``kqw*``, see ``_isolate_windows``) whole: two
queries agree on it when its text agrees, column list included. Two spellings of one numbering join, one
whose CTE exposes ``id, value, rn`` and one that exposes only the columns it needs, then differ in a column
nobody reads. ``prune_window_sources`` removes it.

A select that reads a derived table keeps every row of it when a column is dropped, and so does the derived
table: a window only adds a value to each row, and a plain select adds, removes or repeats none. So an output
the enclosing select never names can go, windows or not. Applies to a derived table when:

* it has a window somewhere inside it (the general ``_prune_derived`` leaves those alone, and this module
  leaves the rest to it), and its select has no ``DISTINCT`` (dropping a column changes which rows collapse),
  ``GROUP BY``, ``HAVING``, ``QUALIFY``, ``ORDER BY``, ``LIMIT``, ``OFFSET``, ``WINDOW`` clause or star, and
  its outputs have distinct names;
* the enclosing select reads columns by name: a read of the derived table's alias, or of a bare name, keeps
  that output (anything under the select counts, a nested subquery included, so a column is never dropped
  that something might read).

A derived table the prover isolated (alias ``kqw*``) names its columns ``kqc<i>`` and ``kqv<i>`` by the order
of their text, so after a column goes the rest are renumbered the same way and the enclosing select's reads
follow. It runs only as a later attempt (``window_joins``).
"""

from __future__ import annotations

import re

from sqlglot import exp

from .ast_utils import FROM_KEY

_NAME = re.compile(r"^kq([cv])\d+$")
_BANNED = ("distinct", "group", "having", "qualify", "limit", "offset", "windows", "with_", "with", "order")


def prune_window_sources(tree: exp.Expression) -> exp.Expression:
    """Prune each derived table of ``tree`` that holds a window (module doc), outermost select first."""

    for select in list(tree.find_all(exp.Select)):
        source_list = [select.args.get(FROM_KEY), *(select.args.get("joins") or [])]
        for holder in source_list:
            source = holder.this if holder is not None else None
            if isinstance(source, exp.Subquery) and source.alias and isinstance(source.this, exp.Select):
                _prune(select, source)
    return tree


def _reads(select: exp.Select, column: exp.Column, alias: str) -> bool:
    """Whether ``column`` (somewhere under ``select``) can read an output of the derived table ``alias``.

    A column of ``select`` itself reads it when it names the alias or no source. A column of another select
    reads it only when it names the alias, or is a bare name inside a subquery that is not a FROM/JOIN source
    (a correlated subquery sees ``select``'s columns); a bare name inside another derived table is that
    table's own column.
    """

    nearest = column.find_ancestor(exp.Select)
    if column.table and column.table.lower() == alias:
        return True
    if nearest is select:
        return not column.table
    if column.table:
        return False
    node = nearest
    while node is not None and node is not select:
        parent = node.parent
        if isinstance(parent, exp.Subquery) and not isinstance(parent.parent, (exp.From, exp.Join)):
            return True
        if isinstance(parent, (exp.Exists, exp.In, exp.Any, exp.All)):
            return True
        node = node.find_ancestor(exp.Select)
    return False


def _prune(select: exp.Select, source: exp.Subquery) -> None:
    inner = source.this
    if any(inner.args.get(k) for k in _BANNED):
        return
    if not any(w for w in inner.find_all(exp.Window)):
        return
    if any(isinstance(e, exp.Star) or (isinstance(e, exp.Column) and isinstance(e.this, exp.Star)) for e in inner.expressions):
        return
    if any(isinstance(star, exp.Star) and not isinstance(star.parent, exp.Count) for star in select.find_all(exp.Star)):
        return
    alias = source.alias.lower()
    used = {c.name.lower() for c in select.find_all(exp.Column) if _reads(select, c, alias)}
    outputs = [e.alias_or_name.lower() for e in inner.expressions]
    if len(set(outputs)) != len(outputs) or "" in outputs:
        return
    keep = [e for e in inner.expressions if e.alias_or_name.lower() in used] or inner.expressions[:1]
    if len(keep) == len(inner.expressions):
        return
    inner.set("expressions", [e.copy() for e in keep])
    if not alias.startswith("kqw"):
        return
    # renumber kqc<i> / kqv<i> by the order of their text, as _isolate_windows names them
    renames: dict[str, str] = {}
    for kind in ("c", "v"):
        items = [e for e in inner.expressions if isinstance(e, exp.Alias) and (m := _NAME.match(e.alias)) and m.group(1) == kind]
        for index, item in enumerate(sorted(items, key=lambda e: e.this.sql())):
            renames[item.alias.lower()] = f"kq{kind}{index}"
    if not renames:
        return
    for item in inner.expressions:
        if isinstance(item, exp.Alias) and item.alias.lower() in renames:
            item.set("alias", exp.to_identifier(renames[item.alias.lower()]))
    for column in select.find_all(exp.Column):
        if column.table.lower() == alias and column.name.lower() in renames:
            column.set("this", exp.to_identifier(renames[column.name.lower()]))
