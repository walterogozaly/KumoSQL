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
  reading (``FORMAT``, ``COLLATE``, approximate aggregates, ``BIGNUMERIC``, ``WEEK(<weekday>)`` other
  than Sunday or Monday, a ``STRUCT`` compared, float literals large enough to overflow).

Nested data (:func:`faithful` with ``columns``, the column types of the tables read): ``UNNEST .. WITH
OFFSET`` counts from 0 (DuckDB's ``WITH ORDINALITY`` from 1), the fields of an ``UNNEST`` of structs are
columns of their own as in BigQuery (``FROM t, UNNEST(t.params) AS p WHERE key = 'x'``), and
``ARRAY_CONCAT`` is ``NULL`` when an argument is (DuckDB skips a ``NULL`` list).

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
from typing import Any, Iterable, Mapping

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

_MACRO_DEFINITIONS = (
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
    # BigQuery writes fractional seconds in groups of three digits (.000100), DuckDB as few as needed (.0001)
    "WHEN typeof(x) IN ('TIMESTAMP WITH TIME ZONE', 'TIMESTAMP', 'TIME') "
    "AND regexp_matches(CAST(x AS VARCHAR), '[.]([0-9]{1,2}|[0-9]{4,5})([^0-9]|$)') "
    f"THEN {_fail('fractional seconds format differently')} "
    "WHEN typeof(x) LIKE 'DECIMAL%' AND contains(CAST(x AS VARCHAR), '.') "
    "THEN rtrim(rtrim(CAST(x AS VARCHAR), '0'), '.') ELSE CAST(x AS VARCHAR) END",
    f"CREATE OR REPLACE TEMP MACRO kumo_bq_avg_arg(x) AS CASE WHEN typeof(x) LIKE 'DECIMAL%' "
    f"THEN {_fail('AVG, STDDEV and VARIANCE of NUMERIC are exact in BigQuery')} ELSE x END",
    # SPLIT with a NULL delimiter is NULL; DuckDB's STR_SPLIT returns the whole string
    "CREATE OR REPLACE TEMP MACRO kumo_bq_split(s, d) AS CASE WHEN d IS NULL THEN NULL ELSE str_split(s, d) END",
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
_MACRO_HEAD = re.compile(r"CREATE OR REPLACE TEMP MACRO (\w+)\(([^)]*)\) AS (.*)", re.DOTALL)
_QUOTED = re.compile(r"'(?:[^']|'')*'")


def _once(statement: str) -> str:
    """The macro with each argument evaluated once.

    DuckDB expands a macro by pasting its argument expressions in wherever the body names them, so
    ``kumo_bq_div(a, b)`` copies ``a`` five times and ``b`` seven: nested calls grow exponentially (a
    variance written with divisions, products and sums took 0.15 s to plan on an empty table, 20 times
    the plain query). Packing the arguments into one struct handed to a lambda binds each once, with the
    same values and types, and ``typeof`` of a field reads the argument's own type.
    """

    name, parameters, body = _MACRO_HEAD.match(statement).groups()
    names = [p.strip() for p in parameters.split(",")]
    # a parameter becomes a field of the lambda's argument (named so that no macro parameter shares its name,
    # which DuckDB would replace inside the struct too), outside string literals only
    fields = {n: f"_f{i}" for i, n in enumerate(names)}
    pattern = re.compile(rf"\b({'|'.join(names)})\b")

    def rename(text: str) -> str:
        return pattern.sub(lambda m: f"_k.{fields[m.group(1)]}", text)

    parts, last = [], 0
    for quoted in _QUOTED.finditer(body):
        parts += [rename(body[last:quoted.start()]), quoted.group()]
        last = quoted.end()
    parts.append(rename(body[last:]))
    packed = ", ".join(f"{fields[n]} := {n}" for n in names)
    return f"CREATE OR REPLACE TEMP MACRO {name}({parameters}) AS list_transform([struct_pack({packed})], _k -> {''.join(parts)})[1]"


# ``kumo_bq_read`` tests ``typeof(x) = 'VARCHAR'`` and a bare NULL argument turns into a VARCHAR field, so it keeps
# the plain form (its argument is a cast's operand, rarely nested deep)
# ``HAVING`` cannot bind a lambda around an aggregate (DuckDB reads the field as an ungrouped column), so there the
# plain form is called, under the name ``<macro>_plain``.
_PACKED = tuple(_MACRO_HEAD.match(m).group(1) for m in _MACRO_DEFINITIONS if "kumo_bq_read(" not in m)
MACROS = tuple(
    statement if "kumo_bq_read(" in statement else _once(statement) for statement in _MACRO_DEFINITIONS
) + tuple(
    statement.replace(f"MACRO {_MACRO_HEAD.match(statement).group(1)}(", f"MACRO {_MACRO_HEAD.match(statement).group(1)}_plain(", 1)
    for statement in _MACRO_DEFINITIONS
    if "kumo_bq_read(" not in statement
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
    # ANY_VALUE(x HAVING MAX/MIN y) becomes ARG_MAX_NULL, which picks another row on NaN and NULL keys
    "HavingMax",
) + tuple(getattr(exp, name) for name in dir(exp) if name.startswith("JSON") and isinstance(getattr(exp, name), type))
_APPROX_NAMES = re.compile(r"^(APPROX_|HLL_COUNT|KLL_|IEEE_DIVIDE$|FORMAT$|COLLATE$|ARRAY_SLICE$)", re.IGNORECASE)
_STRUCT_COMPARISONS = _class("EQ", "NEQ", "LT", "LTE", "GT", "GTE", "NullSafeEQ", "NullSafeNEQ", "In", "Is")


def _fields(node: exp.Struct) -> list[str] | None:
    """The field names of ``STRUCT(.. AS a, .. AS b)``, or ``None`` when a field is unnamed."""

    if not all(isinstance(field, (exp.PropertyEQ, exp.Alias)) for field in node.expressions):
        return None
    return [field.alias_or_name for field in node.expressions]


def _table_row(node: exp.Struct) -> bool:
    """A row of ``FROM UNNEST([STRUCT(.. AS a), STRUCT(.. AS a)])``, an inline table. DuckDB reads the
    rows by field name and BigQuery by position, so every row must name its fields as the first does
    (sqlglot 26 names an unnamed field ``_0``, which DuckDB reads as a new column)."""

    array, unnest = node.parent, node.parent and node.parent.parent
    if not (isinstance(array, exp.Array) and isinstance(unnest, exp.Unnest) and isinstance(unnest.parent, (exp.From, exp.Join))):
        return False
    if len(unnest.expressions) != 1 or unnest.args.get("offset"):
        return False
    rows = [_fields(row) if isinstance(row, exp.Struct) else None for row in array.expressions]
    first = rows[0]
    return bool(first) and len({name.lower() for name in first}) == len(first) and all(row == first for row in rows)


def struct_refusal(tree: exp.Expression, struct_columns: Iterable[str] = ()) -> str | None:
    """A STRUCT may only be read field by field: DuckDB compares and groups whole structs by field
    name (BigQuery by position) and treats NULL fields differently.

    ``struct_columns`` names columns (and UNNEST elements) of STRUCT type in the tables read: one of
    them may be output, grouped, tested ``IS NULL`` or read field by field, but not compared whole
    (``{'a': 1, 'b': NULL} = {'a': 1, 'b': NULL}`` is TRUE in DuckDB and NULL in BigQuery)."""

    names = set()
    for node in tree.find_all(exp.Struct):
        if _table_row(node):
            continue
        if isinstance(node.parent, exp.Dot) and node.arg_key == "this" and _named_fields(node):
            continue  # STRUCT(1 AS a, 2 AS b).b reads one field
        if not (isinstance(node.parent, exp.Alias) and isinstance(node.parent.parent, exp.Select)):
            return "STRUCT outside a select list"
        names.add(node.parent.alias.lower())
    if names and any(isinstance(n, exp.Star) and not isinstance(n.parent, exp.Count) for n in tree.walk()):
        return "STRUCT read by *"
    for column in tree.find_all(exp.Column):
        if column.name.lower() in names and column.table.lower() not in names:
            return "whole STRUCT"
    stored = {name.lower() for name in struct_columns}
    if stored:
        for column in tree.find_all(exp.Column):
            if column.name.lower() in stored and not _whole_struct_allowed(column):
                return "whole STRUCT compared"
    return None


def _named_fields(node: exp.Struct) -> bool:
    """Every field of a STRUCT constructor is named, and no two alike."""

    names = [e.name.lower() if isinstance(e, (exp.PropertyEQ, exp.Alias)) else None for e in node.expressions]
    return bool(names) and None not in names and len(set(names)) == len(names)


def _whole_struct_allowed(column: exp.Column) -> bool:
    """A whole STRUCT value output by a select, grouped on, or tested for NULL."""

    node = column.parent
    if isinstance(node, exp.Alias):
        node = node.parent
    if isinstance(node, (exp.Select, exp.Group)):
        return True
    return isinstance(column.parent, exp.Is) and isinstance(column.parent.expression, exp.Null)


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


def _source_unnest(node: exp.Unnest) -> bool:
    """An ``UNNEST`` read as a table: in ``FROM`` or a join, over one array, with an element name when
    it has ``WITH OFFSET``."""

    if not isinstance(node.parent, (exp.From, exp.Join)) or node.arg_key != "this" or len(node.expressions) != 1:
        return False
    alias = node.args.get("alias")
    columns = alias.args.get("columns") if alias is not None else None
    return not node.args.get("offset") or bool(columns) or bool(alias is not None and alias.name)


_GROUPINGS = _class("GroupingSets", "Rollup", "Cube")
_MOMENTS = _class("Avg", "Stddev", "StddevPop", "StddevSamp", "Variance", "VariancePop")
# an escape sqlglot leaves in a string as the backslash and the letter (hex, unicode, octal, \?, quotes)
_UNDECODED_ESCAPE = re.compile(r"\\[xXuU0-7?'\"`]")
_PARSES = _class("StrToTime", "StrToDate")
_ZONED = _class("TimeToStr", "String")
_LIKES = _class("Like", "ILike")
_ZONE_NAME = re.compile(r"^(Etc/GMT[+-][0-9]{1,2}|[A-Za-z_]+([/-][A-Za-z_]+)*)$")
# a string literal BigQuery may coerce to a date or time when a set operation lines it up with one
_TEMPORAL_TEXT = re.compile(r"^\s*([0-9]{1,4}-[0-9]{1,2}-[0-9]{1,2}|[0-9]{1,2}:[0-9]{2})")


def _value_table_as_table(node: exp.Select) -> bool:
    """``SELECT AS STRUCT/VALUE`` read as a table (a derived table or a CTE): BigQuery reads the struct's
    fields as the columns, DuckDB keeps one struct column."""

    if not node.args.get("kind"):
        return False
    holder = node.parent
    while isinstance(holder, _SET_OPERATIONS):
        holder = holder.parent
    return isinstance(holder, exp.CTE) or (isinstance(holder, exp.Subquery) and isinstance(holder.parent, (exp.From, exp.Join)))


def _group_by_all_without_keys(node: exp.Select) -> bool:
    """``GROUP BY ALL`` with no grouping key and nothing to aggregate (``SELECT 1 ... GROUP BY ALL``): BigQuery
    makes one group, DuckDB drops the grouping and returns every row. With an aggregate in the select list both
    make one group, so that query is translated as written."""

    group = node.args.get("group")
    if group is None or not group.args.get("all"):
        return False
    for item in node.expressions:
        if item.find(exp.AggFunc, exp.Window) is not None or item.find(exp.Column) is not None:
            return False
    return True


def _temporal_text_in_set_operation(node: exp.Expression) -> bool:
    for side in (node.this, node.expression):
        for select in ([side] if isinstance(side, exp.Select) else []):
            for item in select.expressions:
                item = item.this if isinstance(item, exp.Alias) else item
                if isinstance(item, exp.Literal) and item.is_string and _TEMPORAL_TEXT.match(item.this):
                    return True
    return False


def refusal(tree: exp.Expression, struct_columns: Iterable[str] = ()) -> str | None:
    """Why ``tree`` has no faithful DuckDB reading, or ``None``."""

    for node in tree.walk():
        if isinstance(node, _REFUSED):
            return type(node).__name__
        # the cases below were found by the GoogleSQL compliance expected results (tools/googlesql_results_eval.py)
        if isinstance(node, exp.ByteString) and "\\" in str(node.this):
            return "BYTES literal with a backslash"  # sqlglot writes the escapes into an e'..' string as they were
        if (
            isinstance(node, exp.Literal)
            and node.is_string
            and not isinstance(node.parent, exp.RawString)
            and _UNDECODED_ESCAPE.search(node.this)
        ):
            return "string escape sqlglot does not decode"  # \x41, \u20AC, \101, \? and \' stay a backslash
        if isinstance(node, exp.RegexpInstr) and (node.args.get("occurrence") is not None or node.args.get("option") is not None):
            return "REGEXP_INSTR with an occurrence or a return position"  # sqlglot's expansion counts them differently
        if isinstance(node, exp.In) and isinstance(node.this, (exp.Tuple, exp.Struct)) and node.find(exp.Null):
            return "IN over a struct with a NULL"  # BigQuery compares struct fields with NULL as unknown; DuckDB's lists do not
        if isinstance(node, _PARSES):
            # PARSE_DATE/PARSE_TIMESTAMP and CAST .. FORMAT become strptime, which reads format elements and
            # defaults differently; sqlglot also drops the time zone and a CAST's TIME target
            return "PARSE_ or CAST .. FORMAT"
        if isinstance(node, _ZONED) and node.args.get("zone") is not None:
            return f"{node.sql_name()} in a time zone"  # sqlglot drops the zone or the offset
        zone = node.args.get("zone")
        if zone is None and isinstance(node, exp.Datetime) and isinstance(node.expression, exp.Literal):
            zone = node.expression  # DATETIME(timestamp, 'zone')
        if isinstance(zone, exp.Expression) and not (
            isinstance(zone, exp.Literal) and zone.is_string and _ZONE_NAME.match(zone.this)
        ):
            return "time zone that is not a named zone"  # DuckDB reads offsets such as 'UTC+1234' differently
        if isinstance(node, _LIKES) and isinstance(node.expression, (exp.Any, exp.All)) and not isinstance(
            node.expression.this, exp.Tuple
        ):
            return "LIKE ANY/ALL over an array or subquery"  # sqlglot writes LIKE UNNEST(..)
        if isinstance(node, exp.Round) and node.args.get("truncate") is not None:
            return "ROUND with a rounding mode"  # DuckDB's ROUND_EVEN goes through DOUBLE
        if isinstance(node, exp.Pivot) and node.args.get("unpivot"):
            return "UNPIVOT"  # DuckDB puts the name column before the value columns
        if isinstance(node, _GROUPINGS) and any(not isinstance(k, exp.Column) for k in node.expressions):
            # BigQuery reads an integer as a select-list position and matches select items to an expression
            # key differently from DuckDB
            return f"{node.key.upper()} key that is not a column"
        if isinstance(node, exp.Select) and _value_table_as_table(node):
            return "SELECT AS STRUCT/VALUE read as a table"
        if isinstance(node, exp.Select) and _group_by_all_without_keys(node):
            return "GROUP BY ALL without a grouping key"
        if isinstance(node, _SET_OPERATIONS) and _temporal_text_in_set_operation(node):
            return "date or time text in a set operation"  # BigQuery coerces it, DuckDB makes the column text
        if isinstance(node, exp.Anonymous) and _APPROX_NAMES.match(str(node.this)):
            return str(node.this).upper()
        if isinstance(node, exp.Unnest) and node.args.get("offset") and not _source_unnest(node):
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
    return struct_refusal(tree, struct_columns)


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
    if unit in ("MILLISECOND", "MICROSECOND"):
        # DuckDB counts the whole seconds too (56.999 s is 56999 ms); BigQuery only the fraction (999)
        whole = 1000 if unit == "MILLISECOND" else 1000000
        return exp.Paren(this=exp.Mod(this=node, expression=exp.Literal.number(whole)))
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


def _escaped(like: exp.Expression) -> exp.Expression:
    return exp.Escape(this=like, expression=exp.Literal.string("\\"))


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
    if isinstance(node, _LIKES) and not isinstance(node.parent, exp.Escape):
        # BigQuery reads a backslash in a LIKE pattern as an escape (r'a\_b' matches 'a_b'); DuckDB only with ESCAPE
        if isinstance(node.expression, (exp.Any, exp.All)):  # LIKE ANY/ALL ('a', 'b'): one LIKE per pattern
            negate = node.args.get("negate")
            likes = [
                _escaped(type(node)(this=node.this.copy(), expression=p.copy(), negate=negate))
                for p in node.expression.this.expressions
            ]
            joined = exp.or_(*likes) if isinstance(node.expression, exp.Any) else exp.and_(*likes)
            return exp.Paren(this=joined)
        return _escaped(node)
    if isinstance(node, exp.Split) and node.expression is not None:
        return _call("kumo_bq_split", node.this, node.expression)
    if isinstance(node, _MOMENTS):
        # DuckDB averages a DECIMAL (and takes its variance) as a DOUBLE; BigQuery keeps NUMERIC exact (rounded to 9 digits)
        if isinstance(node.this, exp.Distinct):
            node.this.set("expressions", [_call("kumo_bq_avg_arg", e) for e in node.this.expressions])
        else:
            node.set("this", _call("kumo_bq_avg_arg", node.this))
        return None
    if isinstance(node, _SET_OPERATIONS):
        # sqlglot writes a set operation operand without parentheses (A INTERSECT B UNION ALL C)
        for key in ("this", "expression"):
            if isinstance(node.args.get(key), _SET_OPERATIONS):
                node.set(key, exp.Subquery(this=node.args[key]))
        return None
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
    if isinstance(node, exp.ArrayConcat):
        return _null_if_any_null(node, [node.this, *node.expressions])
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


def _nested_names(columns: Mapping[str, str] | None) -> dict[str, Any]:
    """``{lower-case name: NestedType}`` for the nested columns in ``columns`` and every field below them
    (a name with two different types is left out)."""

    from .nested_values import parse_type

    found: dict[str, Any] = {}
    clashes: set[str] = set()

    def note(name: str, value) -> None:
        name = name.lower()
        if name in found and found[name] != value:
            clashes.add(name)
        found[name] = value

    for name, text in (columns or {}).items():
        parsed = parse_type(text) if isinstance(text, str) and text.upper().startswith(("ARRAY", "STRUCT")) else None
        if parsed is None or not parsed.nested:
            continue
        note(name, parsed)
        for path, inner in parsed.walk():
            note(path[-1], inner)
    return {k: v for k, v in found.items() if k not in clashes}


def _array_of(expression: exp.Expression, nested: Mapping[str, Any]):
    """The ARRAY type of ``expression`` (a column or field path) when ``nested`` knows it."""

    name = expression.name if isinstance(expression, (exp.Column, exp.Dot)) else ""
    parsed = nested.get(name.lower()) if name else None
    return parsed if parsed is not None and parsed.kind == "ARRAY" else None


def _unnest_source(node: exp.Unnest, nested: Mapping[str, Any], number: int) -> exp.Expression | None:
    """An ``UNNEST`` in ``FROM`` as a DuckDB derived table with BigQuery's columns: the element under its
    alias, the offset counted from 0, and the fields of a struct element; ``None`` keeps sqlglot's own
    translation (an element of unknown or scalar type and no offset)."""

    if not _source_unnest(node):
        return None
    alias = node.args.get("alias")
    columns = alias.args.get("columns") if alias is not None else None
    element = (columns[0].name if columns else alias.name if alias is not None else "") or ""
    offset_arg = node.args.get("offset")
    offset = (offset_arg.name if isinstance(offset_arg, exp.Expression) and offset_arg.name else "offset") if offset_arg else ""
    array = _array_of(node.expressions[0], nested)
    fields = [n for n, _ in array.element.fields if n] if array is not None and array.element.kind == "STRUCT" else []
    if not offset and not fields:
        return None
    lowered = [f.lower() for f in fields]
    if element.lower() in lowered or offset.lower() in lowered or (element and element.lower() == offset.lower()):
        raise Unfaithful("no DuckDB reading matches BigQuery: an UNNEST field shares its alias's name")
    items = []
    if element:
        items.append(f'__kumo_e AS "{element}"')
    if offset:
        items.append(f'__kumo_o - 1 AS "{offset}"')
    if fields:
        items.append("UNNEST(__kumo_e)")
    source = "UNNEST(__array) WITH ORDINALITY AS __kumo_t(__kumo_e, __kumo_o)" if offset else "UNNEST(__array) AS __kumo_t(__kumo_e)"
    select = _duckdb(f"SELECT {', '.join(items)} FROM {source}", array=node.expressions[0])
    join = node.parent
    if isinstance(join, exp.Join) and join.args.get("side") and join.args.get("on") is None:
        join.set("on", exp.true())  # DuckDB writes a LEFT JOIN of a derived table without ON as a comma join
    return exp.Subquery(this=select, alias=exp.TableAlias(this=exp.to_identifier(f"__kumo_unnest_{number}")))


def faithful(tree: exp.Expression, columns: Mapping[str, str] | None = None) -> exp.Expression:
    """A copy of the BigQuery ``tree`` whose DuckDB SQL evaluates as BigQuery does, or fails where
    BigQuery fails; raises :class:`Unfaithful` when no such reading exists. Run the result on a
    connection prepared by :func:`configure`.

    ``columns`` maps the column names of the tables read to their BigQuery types; with it, nested
    columns are read as BigQuery reads them (struct fields of an ``UNNEST``, whole-struct comparisons
    refused)."""

    nested = _nested_names(columns)
    structs = {name for name, t in nested.items() if t.kind == "STRUCT"}
    for node in tree.find_all(exp.Unnest):
        array = _array_of(node.expressions[0], nested) if len(node.expressions) == 1 else None
        alias = node.args.get("alias")
        names = alias.args.get("columns") if alias is not None else None
        element = (names[0].name if names else alias.name if alias is not None else "") or ""
        if array is not None and array.element.kind == "STRUCT" and element:
            structs.add(element.lower())
    reason = refusal(tree, structs)
    if reason is not None:
        raise Unfaithful(f"no DuckDB reading matches BigQuery: {reason}")
    tree = tree.copy()
    # children before parents, so a replacement wraps already-rewritten operands exactly once
    for number, node in enumerate(reversed(list(tree.find_all(exp.Expression, bfs=False)))):
        parent, key, index = node.parent, node.arg_key, node.index
        replacement = _unnest_source(node, nested, number) if isinstance(node, exp.Unnest) else _rewrite(node)
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
    for call in list(tree.find_all(exp.Anonymous)):
        if str(call.this).lower() in _PACKED and call.find_ancestor(exp.Having) is not None:
            call.set("this", f"{call.this}_plain")
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
