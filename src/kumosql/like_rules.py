"""Drop a ``LIKE`` that another ``LIKE`` on the same column already covers.

``v LIKE 'BL10%'`` implies ``v LIKE 'BL1%'``: every value that starts with ``BL10`` starts with ``BL1``. So
in ``v LIKE 'BL1%' OR v LIKE 'BL10%'`` the second test never adds a row, and in the same pair joined by
``AND`` the first never removes one.

Only patterns that are a literal, or a literal followed by one trailing ``%``, are read; a pattern with
``_``, a backslash, an ``ESCAPE`` clause or a ``%`` anywhere else is left alone, as is ``ILIKE``. The column
must be a plain column, so no volatile value sits on the left. NULL cannot break the rewrite: both tests
see the same column and constant patterns, so when the column is NULL both are NULL, and when it is not
NULL they are ordinary booleans where one implies the other (``A OR B`` is ``A`` and ``A AND B`` is ``B``
when ``B`` implies ``A``).
"""

from __future__ import annotations

from functools import reduce

from sqlglot import exp

_WILDCARDS = ("_", "\\")


def _prefix_pattern(node: exp.Expression) -> tuple[str, str, bool] | None:
    """``(column sql, literal part, has trailing %)`` for ``col LIKE 'literal'`` or ``col LIKE 'literal%'``."""

    if type(node) is not exp.Like or node.args.get("escape") is not None:
        return None
    column, pattern = node.this, node.expression
    if not isinstance(column, exp.Column) or isinstance(column.this, exp.Star):
        return None
    if not (isinstance(pattern, exp.Literal) and pattern.is_string):
        return None
    text = pattern.this
    open_ended = text.endswith("%")
    body = text[:-1] if open_ended else text
    if "%" in body or any(ch in body for ch in _WILDCARDS):
        return None
    return column.sql(dialect="bigquery"), body, open_ended


def _implies(specific: tuple[str, str, bool], general: tuple[str, str, bool]) -> bool:
    """Every value matching ``specific`` also matches ``general`` (same column assumed)."""

    _, small, small_open = specific
    _, big, big_open = general
    if big_open:
        return small.startswith(big)
    return not small_open and small == big


def _drop(parts: list[exp.Expression], keep_specific: bool) -> list[exp.Expression] | None:
    patterns = [_prefix_pattern(p) for p in parts]
    dropped: set[int] = set()
    for i, mine in enumerate(patterns):
        if mine is None:
            continue
        for j, other in enumerate(patterns):
            if i == j or other is None or j in dropped or mine[0] != other[0]:
                continue
            # keep_specific (AND): the broader test is redundant; otherwise (OR) the narrower one is.
            narrow, broad = (other, mine) if keep_specific else (mine, other)
            if not _implies(narrow, broad):
                continue
            if _implies(broad, narrow) and j > i:
                continue  # the same pattern twice: keep the first
            dropped.add(i)
            break
    if not dropped:
        return None
    return [p for k, p in enumerate(parts) if k not in dropped]


def _chain(node: exp.Expression, kind: type) -> list[exp.Expression]:
    if isinstance(node, kind):
        return _chain(node.this, kind) + _chain(node.expression, kind)
    return [node]


def drop_subsumed_like(tree: exp.Expression) -> exp.Expression:
    """Apply the rewrite to every ``OR`` and ``AND`` chain of the tree."""

    if tree.find(exp.Like) is None:
        return tree
    for kind in (exp.Or, exp.And):
        for node in list(tree.find_all(kind)):
            if isinstance(node.parent, kind):
                continue  # only the top of a chain
            if node.parent is None and node is not tree:
                continue  # detached by an earlier replacement
            kept = _drop(_chain(node, kind), keep_specific=kind is exp.And)
            if kept is None:
                continue
            joined = reduce(lambda a, b: kind(this=a, expression=b), kept)
            if node is tree:
                tree = joined
            else:
                node.replace(joined)
    return tree
