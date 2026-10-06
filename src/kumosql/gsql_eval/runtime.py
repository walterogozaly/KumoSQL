"""Runtime structures shared by the compiler, the functions and the executor."""

from __future__ import annotations

from typing import Any, Callable

from . import types as T


class Ctx:
    """State of one evaluation: options, and whether the result came out deterministic."""

    __slots__ = ("tz", "params", "nondeterministic", "inexact", "now", "mode", "max_recursion", "literals_decoded", "reasons")

    def __init__(self, tz, params=None, mode="bigquery", now=None, literals_decoded=False):
        self.tz = tz
        self.params = params or {}
        self.nondeterministic = False
        self.inexact = False
        self.now = now
        self.mode = mode
        self.max_recursion = 500 if mode == "bigquery" else 10000
        self.literals_decoded = literals_decoded
        self.reasons: list[str] = []

    def nondet(self, why: str) -> None:
        self.nondeterministic = True
        if len(self.reasons) < 8 and why not in self.reasons:
            self.reasons.append(why)


class Env:
    """The row an expression is evaluated on, the enclosing query's env (for correlated names) and the CTE results."""

    __slots__ = ("row", "outer", "ctx", "ctes")

    def __init__(self, row, outer, ctx, ctes):
        self.row = row
        self.outer = outer
        self.ctx = ctx
        self.ctes = ctes

    def child(self, row) -> "Env":
        return Env(row, self, self.ctx, self.ctes)


class E:
    """A compiled scalar expression: its static type and a function of the env.

    ``lit`` is ``"null"`` for the NULL literal, ``"literal"`` for other literals and query parameters
    (they coerce more freely, as in GoogleSQL), ``None`` otherwise; ``value`` holds a literal's payload.
    """

    __slots__ = ("type", "fn", "lit", "value", "sub", "exact")

    def __init__(self, type: T.Type, fn: Callable[[Env], Any], lit: str | None = None, value: Any = None, sub: tuple | None = None):
        self.type = type
        self.fn = fn
        self.lit = lit
        self.value = value
        self.exact = None  # a FLOAT64 literal: its decimal value as written (it converts to NUMERIC and BIGNUMERIC from that)
        self.sub = sub  # a STRUCT constructor: the literal info of each field (see ``info``)

    @property
    def info(self):
        """What coercion may use of this expression: ``"null"``, ``"literal"``, ``("struct", field infos)`` or ``None``."""

        if self.sub is not None and self.lit is None:
            return ("struct", self.sub)
        return self.lit

    def __repr__(self) -> str:
        return f"E({self.type}, lit={self.lit})"


def const(type: T.Type, value: Any, lit: str = "literal") -> E:
    fn = lambda env: value  # noqa: E731
    fn.const = True  # a constant: folding its value is safe (an expression typed like NULL that raises, ERROR(), is not)
    return E(type, fn, lit, value)


def is_constant(e: E) -> bool:
    """A literal or the NULL literal itself (not an expression that merely has the NULL literal's coercion rules)."""

    return e.lit == "literal" or (e.lit == "null" and getattr(e.fn, "const", False))


NULL = const(T.INT64, None, "null")
