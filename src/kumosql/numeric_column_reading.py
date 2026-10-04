"""Check a MySQL proof that two different strings differ against an untyped column read as a number.

``WHERE n = 'x' AND n = 'y'`` is empty when ``n`` is a text column, which is how the provers read a column the schema gives
no type. MySQL compares a number with a string as numbers, so for an integer column that holds 0 both strings read as 0 and the
row qualifies. When an untyped column (or columns a join, ``IN`` or derived table ties together) is compared with two different
strings that MySQL reads as the same number, a proof is accepted only if it also holds with those strings replaced by the
number MySQL reads them as: the proof then does not depend on the column being text, and holds for an integer column as well.
A string that is not a small integer after that reading (``'1.5'``) cannot be replaced, so the pair is declined.

Only the ``mysql`` dialect is checked. DuckDB, PostgreSQL and BigQuery refuse to compare an integer column with a string that
is not a number, so no result is returned there to differ.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

import sqlglot
from sqlglot import exp

from . import string_number_compare

DIALECT = "mysql"
_LIMIT = 10**9


@dataclass(frozen=True)
class Reading:
    """The numeric reading of a pair: the rewritten queries, or why none exists."""

    pair: tuple[str, str] | None = None
    problem: str | None = None


def _number(value: int) -> exp.Expression:
    return exp.Neg(this=exp.Literal.number(-value)) if value < 0 else exp.Literal.number(value)


def _rewrite(sql: str, types) -> tuple[str, str | None] | None:
    """``(sql with the strings read as numbers, problem)`` or None when no untyped column meets two strings of one number."""

    try:
        tree = sqlglot.parse_one(sql, read=DIALECT)
    except Exception:  # noqa: BLE001 - the prover reports the parse error itself
        return None
    kinds = string_number_compare._Kinds(tree, types)
    ambiguous = kinds.untyped_text_columns()
    if not ambiguous:
        return None
    for left, right in string_number_compare._pairs(tree):
        for column, other in ((left, right), (right, left)):
            column, other = string_number_compare._unwrap(column), string_number_compare._unwrap(other)
            if not (isinstance(column, exp.Column) and isinstance(other, exp.Literal) and other.is_string):
                continue
            if kinds.find(kinds.key(column)) not in ambiguous:
                continue
            value = string_number_compare.mysql_number(other.this)
            if value != int(value) or abs(value) >= _LIMIT:
                return sql, f"the string {other.this!r} is read by MySQL as {value:g}, which the check cannot replace it with"
            other.replace(_number(int(value)))
    return tree.sql(dialect=DIALECT), None


def reading(left_sql: str, right_sql: str, dialect: str, types=None) -> Reading | None:
    """The numeric reading of a pair, or None when the pair has no untyped column compared with ambiguous strings."""

    if dialect != DIALECT or "'" not in left_sql + right_sql:
        return None
    sides = [_rewrite(sql, types) for sql in (left_sql, right_sql)]
    if sides == [None, None]:
        return None
    for side in sides:
        if side is not None and side[1]:
            return Reading(problem=side[1])
    return Reading(pair=tuple(side[0] if side is not None else sql for side, sql in zip(sides, (left_sql, right_sql))))


def checked(prove: Callable, left_sql: str, right_sql: str, kwargs: dict, decline: Callable):
    """Run ``prove`` and accept a proof only if it also holds under the numeric reading (see the module docstring).

    ``decline(reason)`` builds the "not proven" result.
    """

    found = reading(left_sql, right_sql, kwargs.get("dialect", "bigquery"), kwargs.get("types"))
    if found is None:
        return prove(left_sql, right_sql, **kwargs)
    if found.problem:
        return decline(f"unsupported: {found.problem}")
    result = prove(left_sql, right_sql, **kwargs)
    if not result.proven or prove(*found.pair, **kwargs).proven:
        return result
    return decline(
        "unsupported: the proof needs different strings compared with an untyped column to differ, and MySQL reads them as the same number "
        "when the column is an integer"
    )
