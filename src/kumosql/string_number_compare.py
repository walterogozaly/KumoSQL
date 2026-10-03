"""Decline to prove queries that compare a string with a number.

The provers read a value as a number, a string or a Boolean and call values of different kinds
unequal, so ``WHERE '2' <> 2`` filters nothing out and the query is proven equal to one without the
filter. No engine reads it that way:

- MySQL compares a string with a number as numbers (``'2' = 2`` is true, ``'abc' = 0`` is true);
- DuckDB and PostgreSQL cast the string to the number's type (``'2' = 2`` is true, ``'abc' = 2`` is an error);
- BigQuery rejects the comparison as a type error, so the query cannot run.

:func:`problem` names the first such comparison so the prover returns "not proven". It looks at
equality, ordering, ``BETWEEN``, ``IN`` lists, simple ``CASE`` operands and ``NULLIF``, comparing

- a string literal with a number literal, arithmetic, a numeric ``CAST``, a ``COUNT`` or a column declared numeric;
- a number literal (or the other numeric forms) with a column declared as a string;
- one column compared with a string literal in one place and a number literal in another (``a = 'abc' AND a = 0``
  is not empty on MySQL when ``a`` is an integer column, since ``'abc'`` reads as 0).

The SMT prover reads a plain comparison of that kind as an opaque predicate of its two values, true or false whatever the
engine's conversion rule is, so a pair that uses one the same way on both sides can still be proven (the prover declines the
other forms and the same-column case). It does not fold the comparison, even where the engine's reading is exact (MySQL ``'2' = 2``): a decline is safe on every
engine and the pair stays unproven.
Not covered: an undeclared column compared with a string literal on one side of a join and a number on the other,
and values converted by ``COALESCE``, ``GREATEST`` or ``CASE`` branches.
"""

from __future__ import annotations

import re

import sqlglot
from sqlglot import exp

_INTEGER = ("TINYINT", "SMALLINT", "MEDIUMINT", "INT", "INTEGER", "BIGINT", "INT64", "BYTEINT", "HUGEINT", "UTINYINT", "USMALLINT", "UINTEGER", "UBIGINT", "INT2", "INT4", "INT8")
_FLOAT = ("FLOAT", "FLOAT64", "DOUBLE", "REAL", "FLOAT4", "FLOAT8")
_DECIMAL = ("NUMERIC", "DECIMAL", "BIGNUMERIC", "BIGDECIMAL", "DEC", "NUMBER")
_STRING = ("STRING", "VARCHAR", "TEXT", "CHAR", "NVARCHAR", "NCHAR", "BPCHAR")
_NUMBER_NODES = (exp.Add, exp.Sub, exp.Mul, exp.Div, exp.Mod, exp.IntDiv, exp.Count)
_COMPARISONS = (exp.EQ, exp.NEQ, exp.LT, exp.LTE, exp.GT, exp.GTE, exp.NullSafeEQ, exp.NullSafeNEQ)


def _type_kind(type_sql: str) -> str | None:
    base = re.split(r"[\s(<]", type_sql.strip().upper(), maxsplit=1)[0]
    if base in _INTEGER + _FLOAT + _DECIMAL:
        return "number"
    if base in _STRING:
        return "string"
    return None


def _data_type_kind(data_type: object) -> str | None:
    return _type_kind(data_type.sql()) if isinstance(data_type, exp.DataType) else None


def _column_kinds(types: dict[str, dict[str, str]] | None) -> dict[str, str]:
    """Declared kind of each column name that has one kind in every declared table."""

    kinds: dict[str, set[str | None]] = {}
    for columns in (types or {}).values():
        for name, type_sql in columns.items():
            kinds.setdefault(name.lower(), set()).add(_type_kind(type_sql))
    return {name: found.pop() for name, found in kinds.items() if len(found) == 1 and None not in found}


def _kind(node: exp.Expression, columns: dict[str, str]) -> str | None:
    """``"number"``, ``"string"`` or None (not known) for an operand."""

    while isinstance(node, (exp.Paren, exp.Neg, exp.Alias)):
        node = node.this
    if isinstance(node, exp.Literal):
        return "string" if node.is_string else "number"
    if isinstance(node, exp.Cast):
        return _data_type_kind(node.args.get("to"))
    if isinstance(node, _NUMBER_NODES):
        return "number"
    if isinstance(node, exp.Column):
        return columns.get(node.name.lower())
    return None


def _pairs(tree: exp.Expression):
    """``(left, right)`` operand pairs of every comparison in the tree."""

    for node in tree.walk():
        if isinstance(node, _COMPARISONS):
            yield node.this, node.expression
        elif isinstance(node, exp.Between):
            yield node.this, node.args["low"]
            yield node.this, node.args["high"]
        elif isinstance(node, exp.In) and node.args.get("query") is None:
            for item in node.expressions:
                yield node.this, item
        elif isinstance(node, exp.Case) and node.args.get("this") is not None:
            for branch in node.args.get("ifs") or []:
                yield node.args["this"], branch.this
        elif isinstance(node, exp.Nullif):
            yield node.this, node.expression


def mismatched(node: exp.Expression, types: dict[str, dict[str, str]] | None = None) -> bool:
    """Whether a plain comparison (``=``, ``<>``, ``<``, ``<=``, ``>``, ``>=``, ``<=>``) sets a string against a number."""

    return isinstance(node, _COMPARISONS) and {_kind(node.this, _column_kinds(types)), _kind(node.expression, _column_kinds(types))} == {"string", "number"}


def problem(sql: str, dialect: str = "bigquery", types: dict[str, dict[str, str]] | None = None, plain_ok: bool = False) -> str | None:
    """A description of the first string-versus-number comparison in ``sql``, else None.

    With ``plain_ok`` a plain two-operand comparison is not reported: the SMT prover reads it as an opaque predicate
    of its operands (:meth:`kumosql.smt_equivalence._Compiler._compare`), which is right for any conversion rule.
    """

    try:
        tree = sqlglot.parse_one(sql, read=dialect)
    except Exception:  # noqa: BLE001 - the prover reports the parse error itself
        return None
    columns = _column_kinds(types)
    literal_kinds: dict[str, set[str]] = {}
    for left, right in _pairs(tree):
        kinds = {_kind(left, columns), _kind(right, columns)}
        if kinds == {"string", "number"} and not (plain_ok and isinstance(left.parent, _COMPARISONS)):
            return "a string is compared with a number (the engines convert the string; the prover does not model it)"
        for column, other in ((left, right), (right, left)):
            if isinstance(column, exp.Column) and isinstance(other, exp.Literal):
                literal_kinds.setdefault(column.sql().lower(), set()).add("string" if other.is_string else "number")
    for name, found in literal_kinds.items():
        if len(found) > 1:
            return f"column {name} is compared with both a string and a number (the engines convert the string; the prover does not model it)"
    return None
