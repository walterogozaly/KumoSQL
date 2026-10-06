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


def _strict_set_operations(text: str) -> bool:
    """Whether ``text`` has ``STRICT CORRESPONDING`` set operations (sqlglot reads them exactly as ``BY NAME``: it drops the
    keyword) and no ``BY NAME``, so that a set operation parsed without a mode is a STRICT one.

    Works on tokens, so a STRICT in a string, a comment or a quoted name does not count. A bare STRICT anywhere else (or
    one combined with INNER/LEFT/FULL) is refused: the parse could not tell us what it meant.
    """

    from sqlglot.dialects.bigquery import BigQuery
    from sqlglot.tokens import TokenType

    try:
        tokens = BigQuery().tokenize(text)
    except Exception:  # the parser reports what is wrong with the text
        return False
    setop = (TokenType.UNION, TokenType.INTERSECT, TokenType.EXCEPT)
    quantifier = (TokenType.ALL, TokenType.DISTINCT)
    strict = by_name = False
    for i, token in enumerate(tokens):
        word = token.text.upper()
        if token.token_type is not TokenType.VAR:
            continue
        after_op = i >= 2 and tokens[i - 1].token_type in quantifier and tokens[i - 2].token_type in setop
        if word == "STRICT":
            followed = i + 1 < len(tokens) and tokens[i + 1].token_type is TokenType.VAR and tokens[i + 1].text.upper() == "CORRESPONDING"
            if not (after_op and followed):
                raise Unsupported("STRICT outside STRICT CORRESPONDING (sqlglot drops the keyword)")
            if i >= 3 and tokens[i - 3].text.upper() in ("INNER", "LEFT", "FULL", "OUTER"):
                raise Unsupported("STRICT CORRESPONDING combined with a join-style mode")
            strict = True
        elif word == "BY" and after_op and i + 1 < len(tokens) and tokens[i + 1].text.upper() == "NAME":
            by_name = True
    return strict and not by_name


_EMPTY_STRUCT_FIELD = "__KUMOSQL_EMPTY_STRUCT_FIELD"


def _normalize_empty_struct_syntax(text: str) -> tuple[str, str | None, int]:
    """Make BigQuery's empty STRUCT constructor/type parseable by sqlglot.

    sqlglot tokenizes ``STRUCT<>()`` as ``STRUCT <> ()`` (a comparison), and cannot parse the empty
    ``STRUCT<>`` type used by casts. Only the exact empty angle-bracket form after an unquoted STRUCT token is
    rewritten. Empty constructors become ``STRUCT()``; empty types temporarily receive a private field that is
    removed from the parsed type tree below. Token positions keep text in comments and literals untouched.
    """

    if "STRUCT" not in text.upper():
        return text, None, 0

    from sqlglot.dialects.bigquery import BigQuery
    from sqlglot.tokens import TokenType

    try:
        tokens = BigQuery().tokenize(text)
    except Exception:
        # Let the normal parser path report malformed SQL.
        return text, None, 0

    field = _EMPTY_STRUCT_FIELD
    suffix = 0
    while field.lower() in text.lower():
        suffix += 1
        field = f"{_EMPTY_STRUCT_FIELD}_{suffix}"

    replacements = []
    type_count = 0
    index = 0
    while index + 1 < len(tokens):
        if tokens[index].token_type is not TokenType.STRUCT:
            index += 1
            continue

        first = index + 1
        if tokens[first].token_type is TokenType.NEQ and tokens[first].text == "<>":
            last = first
        elif (
            first + 1 < len(tokens)
            and tokens[first].token_type is TokenType.LT
            and tokens[first + 1].token_type is TokenType.GT
        ):
            last = first + 1
        else:
            index += 1
            continue

        constructor = last + 1 < len(tokens) and tokens[last + 1].token_type is TokenType.L_PAREN
        replacement = "" if constructor else f"<{field} INT64>"
        if not constructor:
            type_count += 1
        replacements.append((tokens[first].start, tokens[last].end + 1, replacement))
        index = last + 1

    for start, end, replacement in reversed(replacements):
        text = text[:start] + replacement + text[end:]
    return text, field if type_count else None, type_count


def _restore_empty_struct_types(tree: exp.Expression, field: str | None, expected: int) -> None:
    """Remove the private parse-only field from each normalized empty STRUCT type."""

    if not expected:
        return
    found = 0
    for node in tree.find_all(exp.DataType):
        kind = node.this.name if hasattr(node.this, "name") else str(node.this)
        fields = node.expressions or []
        if (
            kind.upper() == "STRUCT"
            and len(fields) == 1
            and isinstance(fields[0], exp.ColumnDef)
            and fields[0].name.lower() == (field or "").lower()
        ):
            node.set("expressions", [])
            found += 1
    if found != expected:
        raise Unsupported("sqlglot could not preserve an empty STRUCT type")


def evaluate(sql_or_tree: Any, database: Database | None = None, time_zone: str = "UTC", params: dict | None = None,
             mode: str = "bigquery") -> Result:
    from .compiler import Compiler, EmptyScope

    literals_decoded = False
    strict_certain = False
    if isinstance(sql_or_tree, str):
        from ..string_literals import invalid_literal
        from . import text_guards
        from .literals import decode_literals

        text = sql_or_tree
        if invalid_literal(text):
            raise AnalysisError("Invalid string literal")
        text_guards.check(text)
        strict_certain = _strict_set_operations(text)
        try:
            normalized, empty_struct_field, empty_struct_types = _normalize_empty_struct_syntax(text)
            tree = sqlglot.parse_one(decode_literals(normalized), read="bigquery")
        except sqlglot.errors.ParseError as error:
            raise Unsupported(f"sqlglot cannot parse the query: {str(error)[:120]}") from None
        _restore_empty_struct_types(tree, empty_struct_field, empty_struct_types)
        literals_decoded = True
        if isinstance(tree, exp.Block):  # `SELECT 1;  -- comment`: the statement and a Semicolon carrying the comment
            statements = [e for e in tree.expressions if not isinstance(e, exp.Semicolon)]
            if len(statements) != 1:
                raise Unsupported("several statements")
            tree = statements[0]
    else:
        tree = sql_or_tree
        for lit in tree.find_all(exp.Literal):
            if "\\" in str(lit.this):
                raise Unsupported("backslash in a string literal on the tree path")
    database = database or Database()
    tz = V.zone(time_zone)
    ctx = Ctx(tz, params, mode, literals_decoded=literals_decoded)
    compiler = Compiler(database, tz, params or {}, mode, literals_decoded, strict_certain)
    plan = compiler.query(tree, EmptyScope(), {})
    rows = plan.run(Env((), None, ctx, {}))
    return Result(plan.columns, rows, plan.ordered, not ctx.nondeterministic, ctx.inexact, plan.value_table, list(ctx.reasons))
