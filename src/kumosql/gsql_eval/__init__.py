"""Pure-Python GoogleSQL / BigQuery reference evaluator.

``evaluate(sql, database)`` interprets a query's sqlglot BigQuery tree against in-memory tables and returns the
rows BigQuery returns, or raises :class:`Unsupported` (never an approximation), :class:`AnalysisError` (BigQuery
rejects the query) or :class:`EvalError` (BigQuery fails while running it on this data).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import sqlglot
from sqlglot import exp

from . import types as T
from . import values as V
from .errors import AnalysisError, EvalError, Unsupported
from .runtime import Ctx, Env
from . import expressions as _expressions  # noqa: F401  (registers the expression handlers)

__all__ = ["AnalysisError", "Database", "EvalError", "Result", "Table", "Unsupported", "evaluate"]


@dataclass
class Table:
    """``columns`` is ``[(name, Type)]``; ``rows`` are tuples of payloads (see ``values.py``)."""

    columns: list
    rows: list


class Database:
    def __init__(self, tables: dict[str, Table] | None = None):
        self._tables = {k.lower(): v for k, v in (tables or {}).items()}

    def add(self, name: str, table: Table) -> None:
        self._tables[name.lower()] = table

    def table(self, name: str) -> Table | None:
        return self._tables.get(name.lower())


@dataclass
class Result:
    columns: list  # [(name or None, Type)]
    rows: list
    ordered: bool  # the rows' order is part of the answer (ORDER BY / known order)
    deterministic: bool  # no part of the result depends on an undetermined choice
    inexact: bool  # float arithmetic whose last bits may differ between engines
    value_table: bool = False
    reasons: list = field(default_factory=list)


def evaluate(sql_or_tree: Any, database: Database | None = None, time_zone: str = "UTC", params: dict | None = None,
             mode: str = "bigquery") -> Result:
    from .compiler import Compiler, EmptyScope

    literals_decoded = False
    if isinstance(sql_or_tree, str):
        from ..string_literals import canonical_literals, invalid_literal

        text = sql_or_tree
        if invalid_literal(text):
            raise AnalysisError("Invalid string literal")
        if any(w in text.upper() for w in ("STRICT",)):
            raise Unsupported("STRICT set operation (sqlglot drops the keyword)")
        try:
            tree = sqlglot.parse_one(canonical_literals(text), read="bigquery")
        except sqlglot.errors.ParseError as error:
            raise Unsupported(f"sqlglot cannot parse the query: {str(error)[:120]}") from None
        literals_decoded = True
    else:
        tree = sql_or_tree
        for lit in tree.find_all(exp.Literal):
            if "\\" in str(lit.this):
                raise Unsupported("backslash in a string literal on the tree path")
    database = database or Database()
    tz = V.zone(time_zone)
    ctx = Ctx(tz, params, mode, literals_decoded=literals_decoded)
    compiler = Compiler(database, tz, params or {}, mode, literals_decoded)
    plan = compiler.query(tree, EmptyScope(), {})
    rows = plan.run(Env((), None, ctx, {}))
    return Result(plan.columns, rows, plan.ordered, not ctx.nondeterministic, ctx.inexact, plan.value_table, list(ctx.reasons))
