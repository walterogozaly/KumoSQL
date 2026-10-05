"""Whether one numeric comparison follows from another: ``x > 20`` implies ``x > 10``.

A model that keeps only the groups with ``SUM(s) > 10`` still holds every group a query wants with
``SUM(s) > 20``. ``model_reuse`` uses this to accept the model's HAVING when the query's HAVING is at least as strict
on the same expression, and keeps the query's own condition as a filter over the model. Only comparisons of one
expression with a numeric literal are read; anything else is not implied.
"""

from __future__ import annotations

from decimal import Decimal, InvalidOperation

from sqlglot import exp

_FLIPPED = {">": "<", ">=": "<=", "<": ">", "<=": ">=", "=": "="}
_OPERATORS = {exp.GT: ">", exp.GTE: ">=", exp.LT: "<", exp.LTE: "<=", exp.EQ: "="}


def _number(node: exp.Expression) -> Decimal | None:
    if isinstance(node, exp.Paren):
        return _number(node.this)
    if isinstance(node, exp.Neg):
        inner = _number(node.this)
        return -inner if inner is not None else None
    if isinstance(node, exp.Literal) and node.is_number:
        try:
            return Decimal(node.name)
        except InvalidOperation:
            return None
    return None


def comparison(node: exp.Expression, key) -> tuple[str, str, Decimal] | None:
    """``(key of x, operator, number)`` for ``x OP number`` (or ``number OP x``, flipped), else None."""

    operator = _OPERATORS.get(type(node))
    if operator is None:
        return None
    left, right = node.this, node.expression
    if _number(right) is not None and _number(left) is None:
        return key(left), operator, _number(right)  # type: ignore[return-value]
    if _number(left) is not None and _number(right) is None:
        return key(right), _FLIPPED[operator], _number(left)  # type: ignore[return-value]
    return None


def implies(strong: tuple[str, str, Decimal], weak: tuple[str, str, Decimal]) -> bool:
    """Whether ``strong`` (on the same expression as ``weak``) makes ``weak`` true whenever it is true."""

    if strong[0] != weak[0]:
        return False
    (_, s_op, a), (_, w_op, b) = strong, weak
    if s_op == "=":
        return {">": a > b, ">=": a >= b, "<": a < b, "<=": a <= b, "=": a == b}[w_op]
    if s_op in (">", ">="):
        if w_op == ">":
            return a > b or (a == b and s_op == ">")
        if w_op == ">=":
            return a >= b
        return False
    if w_op == "<":
        return a < b or (a == b and s_op == "<")
    if w_op == "<=":
        return a <= b
    return False


def implied_by_any(model_parts: list[exp.Expression], query_parts: list[exp.Expression], key) -> bool:
    """Whether every model conjunct is a query conjunct (same ``key``) or follows from one comparison of the query."""

    query_keys = {key(q) for q in query_parts}
    strong = [c for c in (comparison(q, key) for q in query_parts) if c is not None]
    for part in model_parts:
        if key(part) in query_keys:
            continue
        weak = comparison(part, key)
        if weak is None or not any(implies(s, weak) for s in strong):
            return False
    return True
