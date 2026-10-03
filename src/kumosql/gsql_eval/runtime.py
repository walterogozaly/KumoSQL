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

    __slots__ = ("type", "fn", "lit", "value")

    def __init__(self, type: T.Type, fn: Callable[[Env], Any], lit: str | None = None, value: Any = None):
        self.type = type
        self.fn = fn
        self.lit = lit
        self.value = value

    def __repr__(self) -> str:
        return f"E({self.type}, lit={self.lit})"


def const(type: T.Type, value: Any, lit: str = "literal") -> E:
    return E(type, lambda env: value, lit, value)


NULL = const(T.INT64, None, "null")
