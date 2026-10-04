"""Read an exact integer written as a string the way MySQL, DuckDB and PostgreSQL read it, where that is safe.

:mod:`kumosql.string_number_compare` declines a string compared with a number, because the engines convert the string and
the provers do not model the conversion. A string that spells a small integer converts the same way on every engine that
converts at all, so these two forms are rewritten before the provers look at the query:

- ``'2' = 2`` (a string literal and a number literal) becomes ``TRUE``, ``'2' < 3`` becomes ``TRUE``, and so on;
- ``t.n = '2'`` where ``t.n`` is declared as an integer type in ``types`` becomes ``t.n = 2``, for ``=``, ``<>``, ``<``, ``<=``,
  ``>``, ``>=``, ``BETWEEN`` bounds and ``IN`` lists whose items are all numbers or such strings.

"A small integer" is an optional minus sign and one to nine digits without a leading zero, so no engine rounds it through a
float, overflows it, or reads leading spaces and zeros differently. Only the ``mysql``, ``duckdb`` and ``postgres`` dialects
are rewritten. BigQuery rejects a string compared with a number as a type error, so a BigQuery query keeps the comparison and
the provers keep declining it; other dialects are left alone.

Nothing else changes: a column without a declared integer type, a decimal or float column, a string with a decimal point or
spaces, and a comparison inside a simple ``CASE`` or ``NULLIF`` stay as written and stay declined.
"""

from __future__ import annotations

import re

import sqlglot
from sqlglot import exp

from . import string_number_compare

DIALECTS = frozenset({"mysql", "duckdb", "postgres"})
_SMALL_INTEGER = re.compile(r"-?(0|[1-9][0-9]{0,8})")
_COMPARISONS = (exp.EQ, exp.NEQ, exp.LT, exp.LTE, exp.GT, exp.GTE)
_COMPARE = {
    exp.EQ: lambda a, b: a == b,
    exp.NEQ: lambda a, b: a != b,
    exp.LT: lambda a, b: a < b,
    exp.LTE: lambda a, b: a <= b,
    exp.GT: lambda a, b: a > b,
    exp.GTE: lambda a, b: a >= b,
}


def _small_integer_string(node: exp.Expression) -> int | None:
    if isinstance(node, exp.Literal) and node.is_string and _SMALL_INTEGER.fullmatch(node.this):
        return int(node.this)
    return None


def _small_integer(node: exp.Expression) -> int | None:
    """The value of a number literal that is a small integer (``-3`` is a negated literal)."""

    sign = 1
    while isinstance(node, exp.Neg):
        sign, node = -sign, node.this
    if isinstance(node, exp.Literal) and not node.is_string and re.fullmatch(r"[0-9]{1,9}", node.this) and (node.this == "0" or node.this[0] != "0"):
        return sign * int(node.this)
    return None


def _number(value: int) -> exp.Expression:
    return exp.Neg(this=exp.Literal.number(-value)) if value < 0 else exp.Literal.number(value)


class _Rewriter:
    def __init__(self, tree: exp.Expression, types):
        self.kinds = string_number_compare._Kinds(tree, types)
        self.changed = False

    def integer_column(self, node: exp.Expression) -> bool:
        if not isinstance(node, exp.Column):
            return False
        key = self.kinds.key(node)
        found = self.kinds.tables.get(key[0]) if key[0] else None
        if found:
            declared = {self.kinds.types[t].get(key[1]) for t in found if t in self.kinds.types}
        elif key[0] in self.kinds.derived:
            return False
        else:
            declared = {columns[key[1]] for columns in self.kinds.types.values() if key[1] in columns}
        return len(declared) == 1 and None not in declared and string_number_compare.is_integer_type(declared.pop())

    def comparison(self, node: exp.Expression) -> exp.Expression | None:
        left, right = node.this, node.expression
        a, b = _small_integer_string(left), _small_integer_string(right)
        if (a is not None and _small_integer(right) is not None) or (b is not None and _small_integer(left) is not None):
            x = a if a is not None else _small_integer(left)
            y = b if b is not None else _small_integer(right)
            return exp.Boolean(this=_COMPARE[type(node)](x, y))
        for column, literal, flipped in ((left, right, False), (right, left, True)):
            value = _small_integer_string(literal)
            if value is not None and self.integer_column(column):
                node.set("expression" if not flipped else "this", _number(value))
                self.changed = True
                return node
        return None

    def between(self, node: exp.Between) -> None:
        if not self.integer_column(node.this):
            return
        for side in ("low", "high"):
            value = _small_integer_string(node.args[side])
            if value is not None:
                node.set(side, _number(value))
                self.changed = True

    def in_list(self, node: exp.In) -> None:
        if node.args.get("query") is not None or node.args.get("unnest") is not None or not self.integer_column(node.this):
            return
        items = node.expressions
        if all(_small_integer_string(i) is not None or _small_integer(i) is not None for i in items):
            node.set("expressions", [_number(_small_integer_string(i)) if _small_integer_string(i) is not None else i for i in items])
            self.changed = True


def normalize(sql: str, dialect: str, types: dict[str, dict[str, str]] | None = None) -> str:
    """``sql`` with exact integer strings read as numbers (see the module docstring), or ``sql`` itself when nothing applies."""

    if dialect not in DIALECTS or "'" not in sql:
        return sql
    try:
        tree = sqlglot.parse_one(sql, read=dialect)
    except Exception:  # noqa: BLE001 - the prover reports the parse error itself
        return sql
    rewriter = _Rewriter(tree, types)
    for node in list(tree.find_all(*_COMPARISONS, exp.Between, exp.In)):
        if isinstance(node, _COMPARISONS):
            folded = rewriter.comparison(node)
            if folded is not None and folded is not node:
                node.replace(folded)
                rewriter.changed = True
        elif isinstance(node, exp.Between):
            rewriter.between(node)
        else:
            rewriter.in_list(node)
    return tree.sql(dialect=dialect) if rewriter.changed else sql
