"""Fold integer division (``DIV``) of two literals: ``10 DIV 2`` is ``5``.

Only non-negative integer literals with a positive divisor fold, so the result
is the same in every reading of ``DIV``: MySQL, BigQuery and DuckDB truncate
toward zero, other engines may floor, and for non-negative operands both are
Python's ``a // b``. A negative operand keeps its ``DIV`` (``-10 DIV 3`` is
``-3`` truncated but ``-4`` floored), and so does a zero divisor (MySQL and
DuckDB return NULL, other engines raise). Decimal and string operands are left
alone (``10.5 DIV 2`` and ``'10' DIV 2`` convert first). The quotient is at
most the dividend, so it stays in the BIGINT range the dividend is in.
"""

from __future__ import annotations

import re

from sqlglot import exp

_LONG_LIMIT = 2**63


def _unsigned(node: exp.Expression) -> int | None:
    node = node.unnest() if isinstance(node, exp.Paren) else node
    if isinstance(node, exp.Literal) and not node.is_string and re.fullmatch(r"\d+", node.name or ""):
        value = int(node.name)
        return value if value < _LONG_LIMIT else None
    return None


def fold_literal_int_div(select: exp.Select) -> exp.Expression | None:
    """Replace each ``a DIV b`` of ``select``'s own expressions whose operands are literals as above."""

    changed = False
    divisions = [d for d in select.find_all(exp.IntDiv) if d.find_ancestor(exp.Select) is select]
    for node in divisions[::-1]:  # innermost first, so (20 DIV 2) DIV 5 folds in one pass
        if node.parent is None:
            continue
        a, b = _unsigned(node.this), _unsigned(node.expression)
        if a is None or b is None or b == 0:
            continue
        target = node.parent if isinstance(node.parent, exp.Paren) else node
        target.replace(exp.Literal.number(a // b))
        changed = True
    return select if changed else None
