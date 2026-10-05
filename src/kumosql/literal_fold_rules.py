"""Fold literals: string functions over literals, and row-wise selects over a union of constant rows.

``UPPER('table')`` is ``'TABLE'``, ``SUBSTRING('table' FROM 1 FOR 2)`` is ``'ta'``, ``CONCAT('ta', 'ble')`` is
``'table'``, and ``'TABLE' = 'VIEW'`` is FALSE. Only results every supported dialect agrees on are folded:
case changes of ASCII text, a substring that starts at 1 or later and is not empty, a concatenation of
non-NULL literals, and equality of two literals (compared exactly, as the solver does).
"""

from __future__ import annotations

from decimal import Decimal

from sqlglot import exp


def _text(node: exp.Expression | None) -> str | None:
    return node.name if isinstance(node, exp.Literal) and node.is_string else None


def _integer(node: exp.Expression | None) -> int | None:
    if isinstance(node, exp.Literal) and not node.is_string and node.name.isdigit():
        return int(node.name)
    return None


def _fold(node: exp.Expression) -> exp.Expression | None:
    if isinstance(node, (exp.Upper, exp.Lower)):
        text = _text(node.this)
        if text is not None and text.isascii():
            return exp.Literal.string(text.upper() if isinstance(node, exp.Upper) else text.lower())
    elif isinstance(node, exp.Substring):
        text, start = _text(node.this), _integer(node.args.get("start"))
        length = node.args.get("length")
        count = None if length is None else _integer(length)
        if text is not None and start is not None and start >= 1 and (length is None or count is not None):
            piece = text[start - 1:] if count is None else text[start - 1:start - 1 + count]
            if piece:
                return exp.Literal.string(piece)
    elif isinstance(node, exp.Concat):
        parts = [_text(part) for part in node.expressions]
        if parts and all(part is not None for part in parts):
            return exp.Literal.string("".join(parts))
    elif isinstance(node, (exp.EQ, exp.NEQ)):
        left, right = _text(node.this), _text(node.expression)
        if left is not None and right is not None:
            return exp.Boolean(this=(left == right) == isinstance(node, exp.EQ))
    return None


def fold_string_literals(tree: exp.Expression) -> exp.Expression:
    """``tree`` with every foldable string expression over literals evaluated."""

    def step(node: exp.Expression) -> exp.Expression:
        return _fold(node) or node

    return tree.transform(step)


def _constant_row(select: exp.Expression) -> tuple | None:
    """The literal items of a ``SELECT 'a' AS x, 1 AS y`` (no source, no clauses), else ``None``."""

    if not isinstance(select, exp.Select) or any(select.args.get(k) for k in ("from_", "from", "where", "group", "having", "distinct", "order", "limit", "offset", "qualify", "windows", "joins", "with_", "with")):
        return None
    if not select.expressions or not all(isinstance(item.unalias(), exp.Literal) or isinstance(item.unalias(), exp.Null) for item in select.expressions):
        return None
    # Numbers compare by value (1 and 1.0 are one row of a UNION), strings exactly.
    return tuple(("n", Decimal(lit.name)) if isinstance(lit, exp.Literal) and not lit.is_string else ("s", lit.name) if isinstance(lit, exp.Literal) else ("null",) for lit in (item.unalias() for item in select.expressions))


_TAIL = ("order", "limit", "offset")


def _union_rows(node: exp.Expression) -> list[exp.Select] | None:
    """The branches of a union of constant rows that holds each row once, else ``None``."""

    # a LIMIT, OFFSET or ORDER BY on the parentheses or on the union keeps some of its rows: the rows are not the whole union
    while isinstance(node, exp.Subquery):
        if node.alias or any(node.args.get(k) for k in _TAIL):
            return None
        node = node.this
    if isinstance(node, exp.Union):
        if any(node.args.get(k) for k in _TAIL + ("with_", "with", "by_name", "side", "kind", "on")):
            return None
        left, right = _union_rows(node.left), _union_rows(node.right)
        if left is None or right is None:
            return None
        rows = left + right
        if node.args.get("distinct", True) and len({_constant_row(r) for r in rows}) != len(rows):
            return None  # a repeated row would collapse; leave it to the general path
        return rows
    return [node] if _constant_row(node) is not None else None


def distribute_over_constant_union(select: exp.Select) -> exp.Expression | None:
    """A row-wise select over a union of constant rows is the union of the select over each row.

    ``SELECT UPPER(x) FROM (SELECT 'a' AS x UNION SELECT 'b') AS t`` reads each constant row once (the rows
    are pairwise different), so it is ``SELECT UPPER(x) FROM (SELECT 'a' AS x) AS t UNION ALL ... 'b'``.
    """

    if any(select.args.get(k) for k in ("group", "having", "distinct", "order", "limit", "offset", "qualify", "windows", "joins", "with_", "with", "laterals")):
        return None
    from_ = select.args.get("from_") or select.args.get("from")
    source = from_.this if from_ is not None else None
    if not isinstance(source, exp.Subquery) or not source.alias or not isinstance(source.this, exp.Union):
        return None
    parts = [*select.expressions, *([select.args["where"]] if select.args.get("where") else [])]
    if any(True for part in parts for _ in part.find_all(exp.AggFunc, exp.Window, exp.Subquery, exp.Star)) or not isinstance(select.parent, (exp.Subquery, exp.CTE, exp.SetOperation, type(None))):
        return None
    rows = _union_rows(source.this)
    if rows is None or len(rows) < 2 or len({len(r.expressions) for r in rows}) != 1:
        return None
    names = [item.alias_or_name.lower() for item in rows[0].expressions]
    if "" in names or len(set(names)) != len(names) or not all(isinstance(i, exp.Alias) for i in rows[0].expressions):
        return None  # a union names its columns after its first branch, which must name them itself
    branches = []
    for row in rows:
        row = row.copy()
        row.set("expressions", [exp.alias_(item.unalias().copy(), name) for item, name in zip(row.expressions, [i.alias for i in rows[0].expressions])])
        branch = select.copy()
        branch.args["from_" if "from_" in branch.args else "from"].set("this", exp.Subquery(this=row.copy(), alias=exp.TableAlias(this=exp.to_identifier(source.alias))))
        branches.append(branch)
    result: exp.Expression = branches[0]
    for branch in branches[1:]:
        result = exp.Union(this=result, expression=branch, distinct=False)
    return result
