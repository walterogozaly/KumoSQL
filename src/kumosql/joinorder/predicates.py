"""Evaluate single-relation SQL predicates on sample rows in plain Python.

Rows are dicts from lower-case column name to a Python value (``None`` for NULL,
``datetime`` for timestamps). Evaluation follows SQL three-valued logic and a
predicate passes only when it is TRUE.
"""

from __future__ import annotations

import datetime as _dt
import re
from typing import Any, Callable

from sqlglot import exp

Row = dict[str, Any]
_UNKNOWN = None


def _parse_time(text: str) -> Any:
    text = text.strip()
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d"):
        try:
            return _dt.datetime.strptime(text, fmt)
        except ValueError:
            pass
    return text


def _literal(node: exp.Literal) -> Any:
    if node.is_string:
        return node.this
    text = node.this
    try:
        return int(text)
    except ValueError:
        return float(text)


def like_regex(pattern: str, case_insensitive: bool = False) -> re.Pattern:
    out = []
    for ch in pattern:
        if ch == "%":
            out.append(".*")
        elif ch == "_":
            out.append(".")
        else:
            out.append(re.escape(ch))
    return re.compile("".join(out) + r"\Z", re.S | (re.I if case_insensitive else 0))


def _cmp(op: str) -> Callable[[Any, Any], Any]:
    def run(a: Any, b: Any) -> Any:
        if a is None or b is None:
            return _UNKNOWN
        if isinstance(a, _dt.datetime) and isinstance(b, str):
            b = _parse_time(b)
        elif isinstance(b, _dt.datetime) and isinstance(a, str):
            a = _parse_time(a)
        try:
            if op == "=":
                return a == b
            if op == "!=":
                return a != b
            if op == "<":
                return a < b
            if op == "<=":
                return a <= b
            if op == ">":
                return a > b
            return a >= b
        except TypeError:
            return str(a) < str(b) if op == "<" else False
    return run


_CMP = {exp.EQ: "=", exp.NEQ: "!=", exp.LT: "<", exp.LTE: "<=", exp.GT: ">", exp.GTE: ">="}


def compile_predicate(node: exp.Expression) -> Callable[[Row], Any]:
    """Return ``fn(row) -> True | False | None`` for a predicate or scalar."""
    if isinstance(node, exp.Paren):
        return compile_predicate(node.this)
    if isinstance(node, exp.Column):
        name = node.name
        return lambda row: row.get(name)
    if isinstance(node, exp.Literal):
        value = _literal(node)
        return lambda row: value
    if isinstance(node, exp.Null):
        return lambda row: None
    if isinstance(node, exp.Boolean):
        value = node.this
        return lambda row: value
    if isinstance(node, exp.Neg):
        inner = compile_predicate(node.this)
        return lambda row: None if inner(row) is None else -inner(row)
    if isinstance(node, (exp.Cast, exp.TryCast)):
        inner = compile_predicate(node.this)
        kind = node.to.this
        if kind in (exp.DataType.Type.TIMESTAMP, exp.DataType.Type.DATETIME,
                    exp.DataType.Type.DATE, exp.DataType.Type.TIMESTAMPTZ):
            return lambda row: (lambda v: _parse_time(v) if isinstance(v, str) else v)(inner(row))
        if kind in (exp.DataType.Type.INT, exp.DataType.Type.BIGINT, exp.DataType.Type.SMALLINT):
            return lambda row: (lambda v: None if v is None else int(v))(inner(row))
        return inner
    if type(node) in _CMP:
        left, right, op = compile_predicate(node.left), compile_predicate(node.right), _cmp(_CMP[type(node)])
        return lambda row: op(left(row), right(row))
    if isinstance(node, exp.And):
        left, right = compile_predicate(node.left), compile_predicate(node.right)

        def _and(row: Row) -> Any:
            a = left(row)
            if a is False:
                return False
            b = right(row)
            if b is False:
                return False
            return True if (a is True and b is True) else _UNKNOWN
        return _and
    if isinstance(node, exp.Or):
        left, right = compile_predicate(node.left), compile_predicate(node.right)

        def _or(row: Row) -> Any:
            a = left(row)
            if a is True:
                return True
            b = right(row)
            if b is True:
                return True
            return False if (a is False and b is False) else _UNKNOWN
        return _or
    if isinstance(node, exp.Not):
        inner = compile_predicate(node.this)
        return lambda row: (lambda v: None if v is None else not v)(inner(row))
    if isinstance(node, exp.Is):
        inner = compile_predicate(node.this)
        if isinstance(node.expression, exp.Null):
            return lambda row: inner(row) is None
        target = compile_predicate(node.expression)
        return lambda row: inner(row) == target(row)
    if isinstance(node, (exp.Like, exp.ILike)):
        inner = compile_predicate(node.this)
        if not isinstance(node.expression, exp.Literal):
            raise ValueError("LIKE needs a literal pattern")
        rx = like_regex(node.expression.this, isinstance(node, exp.ILike))
        return lambda row: (lambda v: None if v is None else rx.match(str(v)) is not None)(inner(row))
    if isinstance(node, exp.Between):
        inner = compile_predicate(node.this)
        low, high = compile_predicate(node.args["low"]), compile_predicate(node.args["high"])
        ge, le = _cmp(">="), _cmp("<=")

        def _between(row: Row) -> Any:
            v = inner(row)
            a, b = ge(v, low(row)), le(v, high(row))
            if a is False or b is False:
                return False
            return True if (a and b) else _UNKNOWN
        return _between
    if isinstance(node, exp.In):
        inner = compile_predicate(node.this)
        if node.args.get("query") is not None:
            raise ValueError("IN (subquery) is not supported")
        values = [compile_predicate(v)(None or {}) for v in node.expressions]
        has_null = any(v is None for v in values)
        options = {v for v in values if v is not None}

        def _in(row: Row) -> Any:
            v = inner(row)
            if v is None:
                return _UNKNOWN
            if v in options:
                return True
            return _UNKNOWN if has_null else False
        return _in
    raise ValueError(f"unsupported predicate: {node.sql()[:80]}")


def compile_filters(nodes: list[exp.Expression]) -> Callable[[Row], bool]:
    fns = [compile_predicate(n) for n in nodes]
    return lambda row: all(fn(row) is True for fn in fns)
