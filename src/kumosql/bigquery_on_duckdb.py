"""Run BigQuery SQL on DuckDB so that a difference DuckDB finds is a difference BigQuery would show.

Every refutation KumoSQL reports from execution (the counterexample searches, bounded replay,
random and targeted databases) runs BigQuery SQL on DuckDB after sqlglot translates it. Where the
two engines disagree on the same SQL, a DuckDB difference can be a false BigQuery refutation, which
is a wrong answer. This module is the one place that closes those gaps, in three ways:

* **session settings** (:func:`configure`): timestamps are read in UTC (sqlglot already spells out
  BigQuery's NULL order, ``NULLS FIRST`` ascending, against DuckDB's default);
* **translation fixes** (:func:`faithful`), each checked against BigQuery itself:
  ``NUMERIC`` is ``DECIMAL(38, 9)`` (sqlglot writes a bare ``DECIMAL``, which is ``DECIMAL(18, 3)``),
  ``EXTRACT(DAYOFWEEK)`` counts Sunday as 1, ``EXTRACT(WEEK)`` is Sunday-based (DuckDB's is ISO),
  ``SUBSTR`` reads a position of 0 or past the left end as 1, ``REGEXP_EXTRACT`` returns ``NULL``
  without a match, and ``CAST(NUMERIC AS STRING)`` drops trailing zeros;
* **guards** that make DuckDB fail wherever BigQuery fails or where no faithful translation exists,
  so the run errors and the pair stays unknown: division, ``MOD`` and ``DIV`` by zero, ``SUM`` past
  ``INT64``, ``POW``/``EXP`` overflow, ``NUMERIC`` products and quotients (BigQuery rounds them to 9
  digits), ``>>`` of a negative number (logical in BigQuery), ``CAST(FLOAT64 AS STRING)``, strings
  cast to numbers, booleans, dates or timestamps in any form the two engines may read differently
  (``'1.0'`` as INT64, ``'nan'``, ``'t'``), and whole queries using constructs without a faithful
  reading (``FORMAT``, ``COLLATE``, approximate aggregates, ``WITH OFFSET``, ``BIGNUMERIC``,
  ``WEEK(<weekday>)`` other than Sunday or Monday, ``STRUCT`` compared or grouped, float literals
  large enough to overflow).

Results are then read the way BigQuery returns them (:func:`bigquery_rows`): a NULL array is ``[]``,
a ``STRUCT`` is its values in order (BigQuery compares structs by position), and a ``DATE`` that
DuckDB returns as a midnight ``TIMESTAMP`` (``DATE_TRUNC``, date plus interval) is a date. Rows that
BigQuery could not return at all (an array holding ``NULL``, a non-finite float) raise
:class:`UnfaithfulOutput`. With these, DuckDB never sees a NaN or an infinity: data generators
produce none, and every operation that could make one fails instead.

Guard errors carry :data:`MARKER`, so a search can tell "BigQuery would fail on this database" (try
the next database) from a query DuckDB cannot run at all (:func:`is_bigquery_failure`).
"""

from __future__ import annotations

import math
import re
from datetime import date, datetime, timedelta
from typing import Any, Iterable

import sqlglot
from sqlglot import exp

MARKER = "BigQuery semantics"

# No NULL-order setting: sqlglot writes BigQuery's order against DuckDB's default (``NULLS FIRST`` on an
# ascending key, nothing on ``ASC NULLS LAST``), so a session default would flip the keys it leaves bare.
SETTINGS = ("SET TimeZone = 'UTC'",)


def _fail(reason: str) -> str:
    return f"error('{MARKER}: {reason}')"


_INTEGERS = "('BIGINT', 'INTEGER', 'HUGEINT', 'SMALLINT', 'TINYINT', 'UBIGINT', 'UINTEGER')"
_NUMERIC = "'DECIMAL(38,9)'"

# String forms both engines read alike, with the same value (BigQuery accepts some more, DuckDB
# many more, such as '1.0' or '1e3' as an integer and 't' or 'yes' as a boolean).
_STRING_FORMS = {
    "int": r"\s*[+-]?[0-9]{1,18}\s*",
    "float": r"\s*[+-]?([0-9]+(\.[0-9]*)?|\.[0-9]+)([eE][+-]?[0-9]{1,2})?\s*",
    "numeric": r"\s*[+-]?[0-9]{1,29}(\.[0-9]{0,9})?\s*",
    "bool": r"(?i)(true|false)",
    "date": r"[0-9]{4}-[0-9]{2}-[0-9]{2}",
    "timestamp": r"[0-9]{4}-[0-9]{2}-[0-9]{2}( [0-9]{2}:[0-9]{2}:[0-9]{2}(\.[0-9]{1,6})?)?",
}

MACROS = (
    f"CREATE OR REPLACE TEMP MACRO kumo_bq_div(a, b) AS CASE WHEN b = 0 THEN {_fail('division by zero')} "
    f"WHEN (typeof(a) = {_NUMERIC} AND (typeof(b) = {_NUMERIC} OR typeof(b) IN {_INTEGERS})) "
    f"OR (typeof(b) = {_NUMERIC} AND typeof(a) IN {_INTEGERS}) THEN {_fail('NUMERIC division rounds to 9 digits')} "
    f"WHEN isinf(CAST(a / b AS DOUBLE)) THEN {_fail('floating point overflow')} ELSE a / b END",
    f"CREATE OR REPLACE TEMP MACRO kumo_bq_mul(a, b) AS CASE WHEN typeof(a) = {_NUMERIC} AND typeof(b) = {_NUMERIC} "
    f"THEN {_fail('NUMERIC product rounds to 9 digits')} "
    f"WHEN isinf(CAST(a * b AS DOUBLE)) THEN {_fail('floating point overflow')} ELSE a * b END",
    # SAFE_DIVIDE is NULL where division fails: a zero divisor or an overflowing quotient.
    "CREATE OR REPLACE TEMP MACRO kumo_bq_safe_div(a, b) AS CASE WHEN b = 0 THEN NULL "
    f"WHEN (typeof(a) = {_NUMERIC} AND (typeof(b) = {_NUMERIC} OR typeof(b) IN {_INTEGERS})) "
    f"OR (typeof(b) = {_NUMERIC} AND typeof(a) IN {_INTEGERS}) THEN {_fail('NUMERIC division rounds to 9 digits')} "
    "WHEN isinf(CAST(a / b AS DOUBLE)) THEN NULL ELSE a / b END",
    f"CREATE OR REPLACE TEMP MACRO kumo_bq_mod(a, b) AS CASE WHEN b = 0 THEN {_fail('division by zero')} ELSE a % b END",
    f"CREATE OR REPLACE TEMP MACRO kumo_bq_intdiv(a, b) AS CASE WHEN b = 0 THEN {_fail('division by zero')} ELSE a // b END",
    f"CREATE OR REPLACE TEMP MACRO kumo_bq_shr(a, b) AS CASE WHEN a < 0 THEN {_fail('>> of a negative number is a logical shift')} "
    "ELSE a >> b END",
    f"CREATE OR REPLACE TEMP MACRO kumo_bq_finite(x) AS CASE WHEN isinf(x) OR isnan(x) THEN {_fail('floating point overflow')} "
    "ELSE x END",
    f"CREATE OR REPLACE TEMP MACRO kumo_bq_int64(x) AS CASE WHEN typeof(x) = 'HUGEINT' "
    f"AND (x > 9223372036854775807 OR x < -9223372036854775808) THEN {_fail('INT64 overflow')} ELSE x END",
    f"CREATE OR REPLACE TEMP MACRO kumo_bq_string(x) AS CASE WHEN typeof(x) IN ('DOUBLE', 'FLOAT') "
    f"THEN {_fail('FLOAT64 to STRING formats differently')} "
    "WHEN typeof(x) LIKE 'DECIMAL%' AND contains(CAST(x AS VARCHAR), '.') "
    "THEN rtrim(rtrim(CAST(x AS VARCHAR), '0'), '.') ELSE CAST(x AS VARCHAR) END",
    f"CREATE OR REPLACE TEMP MACRO kumo_bq_read(x, form) AS CASE WHEN typeof(x) = 'VARCHAR' "
    f"AND NOT regexp_full_match(CAST(x AS VARCHAR), form) THEN {_fail('string read differently by BigQuery')} ELSE x END",
    # a[OFFSET(i)] (base 0) and a[ORDINAL(i)] (base 1): an index outside the array fails, the SAFE_ forms give NULL;
    # DuckDB reads a negative index from the end
    f"CREATE OR REPLACE TEMP MACRO kumo_bq_at(a, i, base) AS CASE WHEN i - base < 0 OR i - base >= len(a) "
    f"THEN {_fail('array index out of range')} ELSE a[i - base + 1] END",
    "CREATE OR REPLACE TEMP MACRO kumo_bq_safe_at(a, i, base) AS CASE WHEN i - base < 0 OR i - base >= len(a) "
    "THEN NULL ELSE a[i - base + 1] END",
    # BigQuery keeps an interval's hours apart from its days (36 hours, not 1 day 12 hours)
    f"CREATE OR REPLACE TEMP MACRO kumo_bq_not_interval(x) AS CASE WHEN typeof(x) = 'INTERVAL' "
    f"THEN {_fail('intervals are split into parts differently')} ELSE x END",
    f"CREATE OR REPLACE TEMP MACRO kumo_bq_substr(s, p) AS substring(s, CASE WHEN p = 0 OR p < -length(s) THEN 1 ELSE p END)",
    f"CREATE OR REPLACE TEMP MACRO kumo_bq_substr3(s, p, n) AS CASE WHEN n < 0 THEN {_fail('negative SUBSTR length')} "
    "ELSE substring(s, CASE WHEN p = 0 OR p < -length(s) THEN 1 ELSE p END, n) END",
)


class Unfaithful(sqlglot.errors.UnsupportedError):
    """The query uses a construct DuckDB cannot evaluate the way BigQuery does."""


class UnfaithfulOutput(ValueError):
    """DuckDB returned a value BigQuery could not return (BigQuery fails on this data instead)."""


def configure(connection) -> None:
    """UTC on ``connection``, and the macros :func:`faithful` calls."""

    for statement in SETTINGS:
        try:
            connection.execute(statement)
        except Exception:  # noqa: BLE001 - TimeZone needs the ICU extension; without it there is no TIMESTAMPTZ
            pass
    for statement in MACROS:
        connection.execute(statement)


def is_bigquery_failure(error: BaseException) -> bool:
    """Whether ``error`` is BigQuery failing on this data (a guard fired or a result BigQuery cannot return)."""

    if isinstance(error, Unfaithful):
        return False
    return isinstance(error, UnfaithfulOutput) or MARKER in str(error)


# --- query-level refusals --------------------------------------------------------------------


def _class(*names: str) -> tuple[type, ...]:
    return tuple(cls for cls in (getattr(exp, name, None) for name in names) if isinstance(cls, type))


_REFUSED = _class(
    "Format", "Collate", "ApproxDistinct", "ApproxQuantile", "ApproxQuantiles", "ApproxTopK", "ApproxTopSum",
    "HllCountMerge", "HllCountExtract", "IeeeDivide",
    # sqlglot's ARRAY_SLICE keeps BigQuery's 0-based bounds; PERCENTILE_CONT ignores RESPECT NULLS
    "ArraySlice", "PercentileCont", "PercentileDisc", "ParseJSON",
) + tuple(getattr(exp, name) for name in dir(exp) if name.startswith("JSON") and isinstance(getattr(exp, name), type))
_APPROX_NAMES = re.compile(r"^(APPROX_|HLL_COUNT|KLL_|IEEE_DIVIDE$|FORMAT$|COLLATE$|ARRAY_SLICE$)", re.IGNORECASE)
_STRUCT_COMPARISONS = _class("EQ", "NEQ", "LT", "LTE", "GT", "GTE", "NullSafeEQ", "NullSafeNEQ", "In", "Is")


def struct_refusal(tree: exp.Expression) -> str | None:
    """A STRUCT may only be read field by field: DuckDB compares and groups whole structs by field
    name (BigQuery by position) and treats NULL fields differently."""

    names = set()
    for node in tree.find_all(exp.Struct):
        if not (isinstance(node.parent, exp.Alias) and isinstance(node.parent.parent, exp.Select)):
            return "STRUCT outside a select list"
        names.add(node.parent.alias.lower())
    if names and any(isinstance(n, exp.Star) and not isinstance(n.parent, exp.Count) for n in tree.walk()):
        return "STRUCT read by *"
    for column in tree.find_all(exp.Column):
        if column.name.lower() in names and column.table.lower() not in names:
            return "whole STRUCT"
    return None


def _capture_groups(pattern: str) -> int:
    return len(re.findall(r"(?<!\\)\((?!\?)", pattern))


_ORDERED_AGGREGATES = _class("ArrayAgg", "GroupConcat")
_CURRENT = _class("CurrentDate", "CurrentDatetime", "CurrentTime", "CurrentTimestamp")


def _ordered(node: exp.Expression) -> bool:
    """An ``ARRAY_AGG``/``STRING_AGG`` with an ``ORDER BY`` of its own or of its window."""

    inner = node.this
    while isinstance(inner, exp.Limit):
        inner = inner.this
    if isinstance(node.args.get("separator"), exp.Order):  # sqlglot 26 hangs STRING_AGG's ORDER BY on the separator
        return True
    return isinstance(inner, exp.Order) or (isinstance(node.parent, exp.Window) and bool(node.parent.args.get("order")))


def refusal(tree: exp.Expression) -> str | None:
    """Why ``tree`` has no faithful DuckDB reading, or ``None``."""

    for node in tree.walk():
        if isinstance(node, _REFUSED):
            return type(node).__name__
        if isinstance(node, exp.Anonymous) and _APPROX_NAMES.match(str(node.this)):
            return str(node.this).upper()
        if isinstance(node, exp.Unnest) and node.args.get("offset"):
            return "UNNEST WITH OFFSET"  # sqlglot writes WITH ORDINALITY, which counts from 1
        if isinstance(node, exp.DataType) and node.this == exp.DataType.Type.BIGDECIMAL:
            return "BIGNUMERIC"
        if isinstance(node, exp.DataType) and node.this in (exp.DataType.Type.JSON, getattr(exp.DataType.Type, "JSONB", None)):
            return "JSON"
        if isinstance(node, _CURRENT) and node.this is not None:
            return f"{node.sql_name()} in a time zone"  # sqlglot 26 drops the zone
        if isinstance(node, exp.Cast) and node.args.get("format"):
            return "CAST .. FORMAT"  # sqlglot drops the format
        if isinstance(node, _ORDERED_AGGREGATES) and not _ordered(node):
            return f"{node.sql_name()} without ORDER BY"  # the order of the elements is BigQuery's to pick
        if isinstance(node, exp.Array) and any(isinstance(e, exp.Query) and not e.args.get("order") for e in node.expressions):
            return "ARRAY(subquery) without ORDER BY"
        if isinstance(node, exp.Literal) and not node.is_string:
            try:
                if abs(float(node.this)) >= 1e150:
                    return "float literal large enough to overflow"
            except ValueError:
                pass
        if isinstance(node, exp.RegexpExtract):
            pattern = node.expression
            if node.args.get("position") or node.args.get("occurrence"):
                return "REGEXP_EXTRACT with a position"
            if not (isinstance(pattern, exp.Literal) and pattern.is_string) or _capture_groups(pattern.this) > 1:
                return "REGEXP_EXTRACT pattern"
    return struct_refusal(tree)


# --- translation fixes and guards --------------------------------------------------------------


def _call(name: str, *args: exp.Expression) -> exp.Expression:
    return exp.Anonymous(this=name, expressions=list(args))


_SET_OPERATIONS = (exp.Union, exp.Intersect, exp.Except)
_READ_FORMS = {
    exp.DataType.Type.BIGINT: "int",
    exp.DataType.Type.INT: "int",
    exp.DataType.Type.DOUBLE: "float",
    exp.DataType.Type.FLOAT: "float",
    exp.DataType.Type.DECIMAL: "numeric",
    exp.DataType.Type.BOOLEAN: "bool",
    exp.DataType.Type.DATE: "date",
    exp.DataType.Type.TIMESTAMP: "timestamp",
    exp.DataType.Type.DATETIME: "timestamp",
}
for _name in ("TIMESTAMPTZ", "TIMESTAMPLTZ"):
    if hasattr(exp.DataType.Type, _name):
        _READ_FORMS[getattr(exp.DataType.Type, _name)] = "timestamp"
_STRING_TYPES = {exp.DataType.Type.TEXT, exp.DataType.Type.VARCHAR}


def _unit(unit: exp.Expression | None) -> str:
    """A date part as text, the same across sqlglot versions: ``WEEK``, ``WEEK(MONDAY)``, ``MONTH``..."""

    if unit is None:
        return ""
    text = unit.name if isinstance(unit, (exp.Var, exp.Literal)) else unit.sql(dialect="bigquery")
    return re.sub(r"[\s'\"]", "", text).upper()


def _week_start(unit: str) -> str | None:
    """``SUNDAY`` or ``MONDAY`` for a week part, ``None`` for any other part."""

    if unit in ("WEEK", "WEEK(SUNDAY)"):
        return "SUNDAY"
    if unit in ("ISOWEEK", "WEEK(MONDAY)"):
        return "MONDAY"
    if unit.startswith("WEEK("):
        raise Unfaithful(f"no DuckDB reading matches BigQuery: {unit}")
    return None


def _duckdb(template: str, **values: exp.Expression) -> exp.Expression:
    """DuckDB SQL ``template`` with the columns named ``__<key>`` replaced by ``values``."""

    tree = sqlglot.parse_one(template, read="duckdb")
    for column in list(tree.find_all(exp.Column)):
        if column.name.startswith("__") and column.name[2:] in values:
            column.replace(values[column.name[2:]].copy())
    return tree


def _week_trunc(value: exp.Expression, start: str) -> exp.Expression:
    """The first day of the week (starting ``start``) holding ``value``, a timestamp; DuckDB's week starts Monday."""

    if start == "MONDAY":
        return _duckdb("DATE_TRUNC('WEEK', __v)", v=value)
    return _duckdb("DATE_TRUNC('WEEK', __v + INTERVAL '1' DAY) - INTERVAL '1' DAY", v=value)


def _extract(node: exp.Extract) -> exp.Expression | None:
    value = node.expression
    unit = _unit(node.this)
    if unit == "DAYOFWEEK":
        return exp.Paren(this=exp.Add(this=node, expression=exp.Literal.number(1)))
    start = _week_start(unit)
    if start is None:
        return None
    if unit == "ISOWEEK":
        return exp.Extract(this=exp.var("WEEK"), expression=value)
    pattern = "%U" if start == "SUNDAY" else "%W"
    return exp.Cast(this=_call("STRFTIME", value, exp.Literal.string(pattern)), to=exp.DataType.build("BIGINT"))


def _null_if_any_null(node: exp.Expression, arguments: list[exp.Expression]) -> exp.Expression:
    """``node`` that is NULL when any argument is (BigQuery's CONCAT, LEAST and GREATEST; older sqlglot
    writes DuckDB's, which skip NULLs)."""

    test = exp.or_(*(exp.Is(this=a.copy(), expression=exp.Null()) for a in arguments))
    return exp.Case(ifs=[exp.If(this=test, true=exp.Null())], default=node)


_TRUNCS = _class("DateTrunc", "DatetimeTrunc", "TimestampTrunc")
_DIFFS = _class("DateDiff", "DatetimeDiff")


def _sums(node: exp.Expression) -> bool:
    """``SUM(..)``, possibly with ``FILTER`` and ``OVER``: an integer sum DuckDB widens past INT64."""

    while isinstance(node, (exp.Window, exp.Filter)):
        node = node.this
    return isinstance(node, exp.Sum)


def _rewrite(node: exp.Expression) -> exp.Expression | None:
    """The faithful replacement of one node (its children already rewritten), or ``None`` to keep it."""

    if isinstance(node, exp.DataType) and node.this == exp.DataType.Type.DECIMAL and not node.expressions:
        return exp.DataType.build("DECIMAL(38, 9)", dialect="duckdb")
    if isinstance(node, exp.Div) and not node.args.get("safe"):
        return _call("kumo_bq_div", node.this, node.expression)
    if isinstance(node, exp.CountIf):  # DuckDB's count_if is NULL over no rows or only NULLs, BigQuery's 0
        return exp.Count(this=exp.Case(ifs=[exp.If(this=node.this, true=exp.Literal.number(1))]))
    if isinstance(node, exp.SafeDivide):
        return _call("kumo_bq_safe_div", node.this, node.expression)
    if isinstance(node, exp.Mul):
        return _call("kumo_bq_mul", node.this, node.expression)
    if isinstance(node, exp.Mod):
        return _call("kumo_bq_mod", node.this, node.expression)
    if isinstance(node, exp.IntDiv):
        return _call("kumo_bq_intdiv", node.this, node.expression)
    if isinstance(node, getattr(exp, "BitwiseRightShift", ())):
        return _call("kumo_bq_shr", node.this, node.expression)
    if isinstance(node, (exp.Pow, exp.Exp)):
        return _call("kumo_bq_finite", node)
    if _sums(node) and not isinstance(node.parent, (exp.Window, exp.Filter)) and not (
        isinstance(node, exp.Sum) and isinstance(node.parent, exp.Filter)
    ):
        return _call("kumo_bq_int64", node)
    if isinstance(node, exp.Bracket) and len(node.expressions) == 1 and not (
        isinstance(node.expressions[0], exp.Literal) and node.expressions[0].is_string
    ):
        base = exp.Literal.number(1 if node.args.get("offset") == 1 else 0)
        return _call("kumo_bq_safe_at" if node.args.get("safe") else "kumo_bq_at", node.this, node.expressions[0], base)
    if isinstance(node, exp.Extract):
        node.set("expression", _call("kumo_bq_not_interval", node.expression))
        return _extract(node)
    if isinstance(node, _TRUNCS):
        start = _week_start(_unit(node.args.get("unit")))
        if start is None:
            return None
        truncated = _week_trunc(node.this, start)
        return exp.Cast(this=truncated, to=exp.DataType.build("DATE")) if isinstance(node, exp.DateTrunc) else truncated
    if isinstance(node, _DIFFS):
        start = _week_start(_unit(node.args.get("unit")))
        if start is None:
            return None
        # BigQuery counts the week boundaries crossed: whole weeks between the two weeks' first days
        return _duckdb(
            "DATE_DIFF('DAY', CAST(__s AS DATE), CAST(__e AS DATE)) // 7",
            s=_week_trunc(node.expression, start),
            e=_week_trunc(node.this, start),
        )
    if isinstance(node, exp.Concat):
        return _null_if_any_null(node, node.expressions)
    if isinstance(node, (exp.Least, exp.Greatest)):
        return _null_if_any_null(node, [node.this, *node.expressions])
    if isinstance(node, exp.Substring):
        start = node.args.get("start")
        length = node.args.get("length")
        if start is None:
            return None
        if length is None:
            return _call("kumo_bq_substr", node.this, start)
        return _call("kumo_bq_substr3", node.this, start, length)
    if isinstance(node, exp.RegexpExtract):
        test = exp.RegexpLike(this=node.this.copy(), expression=node.expression.copy())
        return exp.Case(ifs=[exp.If(this=test, true=node)])
    if isinstance(node, (exp.Cast, exp.TryCast)):
        target = node.to.this
        if target in _STRING_TYPES:
            return _call("kumo_bq_string", node.this)
        form = _READ_FORMS.get(target)
        if form is not None and not (isinstance(node.this, exp.Literal) and not node.this.is_string):
            node.set("this", _call("kumo_bq_read", node.this, exp.Literal.string(_STRING_FORMS[form])))
        return None
    return None


def faithful(tree: exp.Expression) -> exp.Expression:
    """A copy of the BigQuery ``tree`` whose DuckDB SQL evaluates as BigQuery does, or fails where
    BigQuery fails; raises :class:`Unfaithful` when no such reading exists. Run the result on a
    connection prepared by :func:`configure`."""

    reason = refusal(tree)
    if reason is not None:
        raise Unfaithful(f"no DuckDB reading matches BigQuery: {reason}")
    tree = tree.copy()
    # children before parents, so a replacement wraps already-rewritten operands exactly once
    for node in reversed(list(tree.find_all(exp.Expression, bfs=False))):
        parent, key, index = node.parent, node.arg_key, node.index
        replacement = _rewrite(node)
        if replacement is None or replacement is node:
            continue
        if parent is None:
            tree = replacement
            continue
        slot = parent.args.get(key)
        if isinstance(slot, list):
            slot[index] = replacement
        else:
            parent.args[key] = replacement
        replacement.parent, replacement.arg_key, replacement.index = parent, key, index if isinstance(slot, list) else None
    return tree


def to_duckdb_sql(tree: exp.Expression) -> str:
    return faithful(tree).sql(dialect="duckdb")


# --- reading results --------------------------------------------------------------------------


def _value(value: Any) -> Any:
    if isinstance(value, float):
        if not math.isfinite(value):
            raise UnfaithfulOutput(f"{MARKER}: DuckDB returned {value!r}, which BigQuery fails with an error to produce")
        return value
    if isinstance(value, list):
        if any(v is None for v in value):
            raise UnfaithfulOutput(f"{MARKER}: BigQuery cannot return an array with a NULL element")
        return tuple(_value(v) for v in value) or None
    if isinstance(value, dict):
        return tuple(_value(v) for v in value.values())
    if isinstance(value, timedelta):
        raise UnfaithfulOutput(f"{MARKER}: BigQuery splits an interval into parts differently")
    if isinstance(value, datetime) and value.tzinfo is None and value.time() == datetime.min.time():
        return value.date()
    return value


def bigquery_rows(rows: Iterable[Iterable[Any]]) -> list[tuple]:
    """``rows`` as BigQuery would return them; raises :class:`UnfaithfulOutput` for a row it could not."""

    return [tuple(_value(v) for v in row) for row in rows]


__all__ = [
    "MARKER",
    "SETTINGS",
    "MACROS",
    "Unfaithful",
    "UnfaithfulOutput",
    "bigquery_rows",
    "configure",
    "faithful",
    "is_bigquery_failure",
    "refusal",
    "struct_refusal",
    "to_duckdb_sql",
]
