"""Drop casts that cannot change a value, and fold constant CASE conditions.

* ``CAST(e AS T)`` is ``e`` when ``e`` already has ``T``'s kind: an integer
  expression cast to an integer type at least as wide (``CAST(1 AS BIGINT)``,
  ``CAST(SUM(int_col) AS INTEGER)``), a DATE cast to DATE, a boolean cast to
  BOOLEAN. As in ``_fold_identity_casts``, overflow is excluded: the width of
  ``SUM`` or ``a + b`` is read as the width of its operands, and an integer cast
  to ``INT`` or wider is taken to fit (a cast to ``TINYINT`` or ``SMALLINT``
  folds only when the input is declared that narrow).
* ``CAST(CAST(x AS T) AS T)`` is ``CAST(x AS T)``.
* A numeric literal cast to ``DECIMAL(p, s)`` that it fits exactly is that
  decimal literal (``CAST(5 AS DECIMAL(11, 1))`` is ``5.0``; not in BigQuery,
  where a decimal literal is a FLOAT64 and ``NUMERIC`` is exact), and an integer
  string literal cast to an integer type is the integer (``CAST('12' AS
  SIGNED)`` is ``12``).
* A strict comparison of a ``COUNT`` with an integer literal is the
  non-strict one with the literal moved by one: ``COUNT(x) > 1`` is
  ``COUNT(x) >= 2``. (Other integers keep their comparisons: the SMT reads
  them as reals, where ``x > 50`` and ``x <= 50`` stay complementary.)
* An ``INT`` (or narrower) expression cast to ``DOUBLE``, a wide enough
  ``DECIMAL``, or ``FLOAT`` (32-bit, so only for integers of at most 7 digits:
  16777217 is not a ``FLOAT``) keeps its order and its ties (the cast is exact and
  one-to-one there), so as an ``ORDER BY`` key it is the expression itself; and
  such a cast is NULL exactly when its input is. Here the operands' width is not
  enough: ``i * i`` of two ``INT`` values can need 19 digits, and a ``SUM`` any
  number, so arithmetic is bounded by its operands' sizes and ``SUM`` keeps its cast.
* ``ROUND(x)`` is ``ROUND(x, 0)``.
* ``1 * x`` is ``x`` where ``x`` is already a number (an aggregate or arithmetic)
  or is read as one (inside arithmetic or ``SUM``/``AVG``), in dialects whose
  ``/`` never divides integers to an integer (literals like ``1.0`` are read
  as ``1`` earlier, so ``x * 1.0`` cannot change a later division there).
* ``<literal> IS NULL`` is FALSE and ``NULL IS NULL`` is TRUE. A ``CASE``
  branch whose condition is FALSE is dropped, and one whose condition is TRUE
  becomes the ``ELSE`` (later branches can never be reached).

Types come from the declared column types, read in the query's dialect (every
BigQuery integer type is ``INT64``, every Snowflake one ``NUMBER(38, 0)``), and
are followed through derived tables. An expression whose type is not known
keeps its cast.
"""

from __future__ import annotations

import re

from sqlglot import exp

_INTEGER_DIGITS = {"TINYINT": 3, "SMALLINT": 5, "MEDIUMINT": 8, "INT": 10, "INTEGER": 10, "BIGINT": 19, "INT64": 19}
# Dialects whose integer names all mean one type: BigQuery's INT, SMALLINT, TINYINT, BYTEINT, ...
# are INT64, and Snowflake's are NUMBER(38, 0).
_ONE_INTEGER_TYPE = {"bigquery": 19, "snowflake": 38}
_LONG_LIMIT = 2**63


def _type_name(datatype: exp.DataType) -> str:
    this = datatype.this
    return (this.name if isinstance(this, exp.DataType.Type) else str(this)).upper()


def _declared(type_sql: str, dialect: str) -> tuple[str, int] | None:
    """``(kind, digits)`` of a declared column type: ``int``, ``date`` or ``bool``."""

    try:
        parsed = exp.DataType.build(type_sql, dialect=dialect)
    except Exception:  # noqa: BLE001 - an unreadable type is an unknown type
        return None
    if re.match(r"\s*int8\b", type_sql, re.I) and _type_name(parsed) != "BIGINT":
        return None  # sqlglot reads ClickHouse's 8-bit Int8 here, but MySQL's INT8 is a BIGINT
    return _target(parsed, dialect)


def _target(datatype: exp.DataType, dialect: str) -> tuple[str, int] | None:
    name = _type_name(datatype)
    if name in _INTEGER_DIGITS:
        return "int", _ONE_INTEGER_TYPE.get(dialect, _INTEGER_DIGITS[name])
    if name == "DATE":
        return "date", 0
    if name == "BOOLEAN":
        return "bool", 0
    return None


_PREDICATES = (exp.EQ, exp.NEQ, exp.LT, exp.LTE, exp.GT, exp.GTE, exp.And, exp.Or, exp.Not, exp.Is, exp.In, exp.Exists, exp.Like, exp.ILike, exp.Between, exp.NullSafeEQ, exp.NullSafeNEQ)


def _sources(select: exp.Select) -> list[exp.Expression]:
    from_ = select.args.get("from_") or select.args.get("from")
    return ([from_.this] if from_ is not None else []) + [j.this for j in select.args.get("joins") or []]


def _column_type(select: exp.Select, column: exp.Column, types: dict, depth: int, dialect: str, bound: bool) -> tuple[str, int] | None:
    name = column.name.lower()
    found = []
    for source in _sources(select):
        if column.table and (source.alias_or_name or "").lower() != column.table.lower():
            continue
        if isinstance(source, exp.Table):
            parts = [p.name for p in (source.args.get("catalog"), source.args.get("db"), source.this) if p is not None]
            declared = types.get(".".join(parts).lower())
            if declared is None:
                return None  # an unknown table may hold the column
            if name in declared:
                found.append(_declared(declared[name], dialect))
        elif isinstance(source, exp.Subquery) and isinstance(source.this, exp.Select):
            inner = source.this
            items = [i for i in inner.expressions if i.alias_or_name.lower() == name]
            if any(isinstance(i.unalias(), exp.Star) for i in inner.expressions):
                return None
            if len(items) > 1:
                return None
            if items:
                found.append(expression_type(items[0].unalias(), inner, types, depth + 1, dialect, bound))
        else:
            return None  # VALUES, UNNEST, set operations: not followed
    return found[0] if len(found) == 1 else None


def expression_type(node: exp.Expression, select: exp.Select, types: dict, depth: int = 0, dialect: str = "bigquery", bound: bool = False) -> tuple[str, int] | None:
    """``(kind, digits)`` of ``node`` read in ``select``'s scope, or ``None`` when unknown.

    An integer's ``digits`` is the width of its operands for ``SUM`` and arithmetic (overflow is
    excluded), which says nothing about its size: ``2000000000 * 2000000000`` has 19 digits. With
    ``bound`` it is a true bound instead, every value being below ``10 ** digits`` in magnitude.
    """

    if depth > 8 or node is None:
        return None
    if isinstance(node, exp.Paren):
        return expression_type(node.this, select, types, depth + 1, dialect, bound)
    if isinstance(node, exp.Literal):
        if not node.is_string and re.fullmatch(r"\d+", node.name or "") and int(node.name) < _LONG_LIMIT:
            return "int", len(node.name.lstrip("0") or "0")
        return None
    if isinstance(node, exp.Neg):
        inner = expression_type(node.this, select, types, depth + 1, dialect, bound)
        return inner if inner and inner[0] == "int" else None
    if isinstance(node, exp.Boolean) or isinstance(node, _PREDICATES):
        return "bool", 0
    if isinstance(node, exp.Column):
        if not isinstance(node.this, exp.Identifier):
            return None
        return _column_type(select, node, types, depth, dialect, bound)
    if isinstance(node, exp.Cast) and not isinstance(node, exp.TryCast) and isinstance(node.args.get("to"), exp.DataType):
        target = _target(node.args["to"], dialect)
        if target is None:
            return None
        if target[0] == "bool" and expression_type(node.this, select, types, depth + 1, dialect, bound) != ("bool", 0):
            return None  # what an integer casts to differs between engines
        return target
    if isinstance(node, (exp.Add, exp.Sub, exp.Mul, exp.IntDiv)):
        a = expression_type(node.this, select, types, depth + 1, dialect, bound)
        b = expression_type(node.expression, select, types, depth + 1, dialect, bound)
        if not (a and b and a[0] == b[0] == "int"):
            return None
        if bound and isinstance(node, exp.Mul):
            return "int", a[1] + b[1]
        if bound and isinstance(node, (exp.Add, exp.Sub)):
            return "int", max(a[1], b[1]) + 1
        return "int", max(a[1], b[1])  # a true bound for DIV too: |a DIV b| <= |a|
    if isinstance(node, (exp.Sum, exp.Min, exp.Max)):
        argument = node.this
        if isinstance(argument, exp.Distinct):
            if len(argument.expressions) != 1:
                return None
            argument = argument.expressions[0]
        inner = expression_type(argument, select, types, depth + 1, dialect, bound)
        if isinstance(node, exp.Sum):
            return inner if inner and inner[0] == "int" and not bound else None  # a sum of many rows has no bound
        return inner
    if isinstance(node, exp.Count):
        return "int", 19
    if isinstance(node, (exp.Case, exp.Coalesce)):
        if isinstance(node, exp.Case):
            if node.this is not None:
                return None
            values = [branch.args.get("true") for branch in node.args.get("ifs") or []] + [node.args.get("default")]
        else:
            values = [node.this] + list(node.expressions)
        kinds = [expression_type(v, select, types, depth + 1, dialect, bound) for v in values if v is not None and not isinstance(v, exp.Null)]
        if not kinds or any(k is None for k in kinds) or len({k[0] for k in kinds}) != 1:
            return None
        return kinds[0][0], max(k[1] for k in kinds)
    return None


def _decimal_params(datatype: exp.DataType) -> tuple[int, int] | None:
    if _type_name(datatype) not in ("DECIMAL", "NUMERIC"):
        return None
    sizes = [e.this.this for e in datatype.expressions if isinstance(e, exp.DataTypeParam) and isinstance(e.this, exp.Literal)]
    if len(sizes) != 2:
        return None
    try:
        return int(sizes[0]), int(sizes[1])
    except ValueError:
        return None


_INTEGER_MAX = {"TINYINT": 2**7, "SMALLINT": 2**15, "MEDIUMINT": 2**23, "INT": 2**31, "INTEGER": 2**31, "BIGINT": 2**63, "INT64": 2**63}


def _fits(value: int, datatype: exp.DataType) -> bool:
    """``value`` (or its negation) is in range of the integer type: magnitude below the type's bound."""

    return value < _INTEGER_MAX.get(_type_name(datatype), 0)


def _literal_cast(cast: exp.Cast, dialect: str) -> exp.Expression | None:
    """A literal cast to a type it fits exactly, as a literal of that type."""

    value, to = cast.this, cast.args["to"]
    negative = isinstance(value, exp.Neg)
    if negative:
        value = value.this
    if not isinstance(value, exp.Literal):
        return None
    target = _target(to, dialect)
    if value.is_string:
        match = re.fullmatch(r"(-?)(\d+)", value.this or "")
        if negative or target is None or target[0] != "int" or match is None:
            return None
        digits = match.group(2).lstrip("0") or "0"
        if not _fits(int(digits), to):
            return None
        number = exp.Literal.number(digits)
        return exp.Neg(this=number) if match.group(1) and digits != "0" else number
    if target is not None and target[0] == "int" and re.fullmatch(r"\d+", value.this or ""):
        return (exp.Neg(this=value.copy()) if negative else value.copy()) if _fits(int(value.this), to) else None
    params = _decimal_params(to)
    match = re.fullmatch(r"(\d+)(?:\.(\d*))?", value.this or "")
    if params is None or match is None or dialect == "bigquery":
        return None  # a BigQuery decimal literal is a FLOAT64: ``CAST(0.1 AS NUMERIC(2, 1)) + CAST(0.2 AS NUMERIC(2, 1))`` is 0.3, ``0.1 + 0.2`` is not
    precision, scale = params
    whole = match.group(1).lstrip("0") or "0"
    fraction = (match.group(2) or "").rstrip("0")
    if len(fraction) > scale or (len(whole) if whole != "0" else 0) > precision - scale:
        return None
    text = whole + ("." + fraction.ljust(scale, "0") if scale else "")
    number = exp.Literal.number(text)
    return exp.Neg(this=number) if negative else number


# Integers a floating type holds exactly, as digits: a DOUBLE (binary64) every one up to 2**53, so
# any of 15 digits, and a FLOAT (binary32 in DuckDB, MySQL, Spark, Postgres' REAL) every one up to
# 2**24, so any of 7 digits (16777217 is not one). A precision argument is not read: Postgres's
# FLOAT(24), parsed as DOUBLE, is a REAL.
_FLOAT_DIGITS = {"DOUBLE": 15, "FLOAT": 7}


def _exact_numeric_cast(cast: exp.Expression, select: exp.Select, types: dict, dialect: str) -> exp.Expression | None:
    """The input of a cast from an INT-or-narrower expression to an exact-on-it numeric type, else ``None``."""

    if not isinstance(cast, exp.Cast) or isinstance(cast, exp.TryCast) or not isinstance(cast.args.get("to"), exp.DataType):
        return None
    have = expression_type(cast.this, select, types, dialect=dialect, bound=True)
    if have is None or have[0] != "int" or have[1] > _INTEGER_DIGITS["INT"]:
        return None
    name = _type_name(cast.args["to"])
    params = _decimal_params(cast.args["to"])
    floating = name in _FLOAT_DIGITS and not cast.args["to"].expressions and _FLOAT_DIGITS[name] >= have[1]
    if floating or name in _INTEGER_DIGITS and _INTEGER_DIGITS[name] >= have[1] or (params and params[0] - params[1] >= have[1]):
        return cast.this
    return None


def _fold_cast(cast: exp.Cast, select: exp.Select, types: dict, dialect: str) -> exp.Expression | None:
    if isinstance(cast, exp.TryCast) or not isinstance(cast.args.get("to"), exp.DataType):
        return None
    to = cast.args["to"]
    inner = cast.this.unnest() if isinstance(cast.this, exp.Paren) else cast.this
    if isinstance(inner, exp.Cast) and not isinstance(inner, exp.TryCast) and isinstance(inner.args.get("to"), exp.DataType) and inner.args["to"].sql() == to.sql():
        return inner.copy()
    literal = _literal_cast(cast, dialect)
    if literal is not None:
        return literal
    if isinstance(inner, exp.Literal) or isinstance(inner, exp.Neg) and isinstance(inner.this, exp.Literal):
        return None  # a literal folds only when it fits the type exactly (above)
    target = _target(to, dialect)
    if target is None:
        return None
    have = expression_type(inner, select, types, dialect=dialect)
    if have is None or have[0] != target[0]:
        return None
    if target[0] == "int" and have[1] > target[1] and target[1] < _INTEGER_DIGITS["INT"]:
        return None  # narrowing to TINYINT or SMALLINT is a real range check
    return cast.this.copy()


def _constant_is_null(node: exp.Is) -> exp.Expression | None:
    if not isinstance(node.expression, exp.Null):
        return None
    value = node.this.unnest() if isinstance(node.this, exp.Paren) else node.this
    if isinstance(value, exp.Null):
        return exp.true()
    if isinstance(value, (exp.Literal, exp.Boolean)) or isinstance(value, exp.Neg) and isinstance(value.this, exp.Literal):
        return exp.false()
    return None


def _truth(node: exp.Expression) -> bool | None:
    node = node.unnest() if isinstance(node, exp.Paren) else node
    if isinstance(node, exp.Boolean):
        return bool(node.this)
    if isinstance(node, exp.Not):
        inner = _truth(node.this)
        return None if inner is None else not inner
    return None


def _fold_case(case: exp.Case) -> exp.Expression | None:
    if case.this is not None:
        return None
    ifs = case.args.get("ifs") or []
    kept = []
    default = case.args.get("default")
    changed = False
    for branch in ifs:
        truth = _truth(branch.this)
        if truth is False:
            changed = True
            continue
        if truth is True:
            default = branch.args.get("true")
            changed = True
            break
        kept.append(branch)
    if not changed:
        return None
    if not kept:
        return default.copy() if default is not None else exp.Null()
    rebuilt = case.copy()
    rebuilt.set("ifs", [b.copy() for b in kept])
    rebuilt.set("default", default.copy() if default is not None else None)
    return rebuilt


def _number(value: int) -> exp.Expression:
    return exp.Neg(this=exp.Literal.number(-value)) if value < 0 else exp.Literal.number(value)


def _int_literal(node: exp.Expression) -> int | None:
    node = node.unnest() if isinstance(node, exp.Paren) else node
    if isinstance(node, exp.Neg):
        inner = _int_literal(node.this)
        return None if inner is None else -inner
    if isinstance(node, exp.Literal) and not node.is_string and re.fullmatch(r"\d+", node.name or "") and int(node.name) < _LONG_LIMIT:
        return int(node.name)
    return None


def _non_strict(node: exp.Expression, select: exp.Select, types: dict) -> exp.Expression | None:
    """``c > n`` is ``c >= n + 1`` and ``c < n`` is ``c <= n - 1`` for a ``COUNT`` ``c`` (either side)."""

    left, right = node.this, node.expression
    for value, other, literal_on_right in ((_int_literal(right), left, True), (_int_literal(left), right, False)):
        if value is None or _int_literal(other) is not None:
            continue
        if not isinstance(other.unnest() if isinstance(other, exp.Paren) else other, exp.Count):
            return None  # the SMT reads other numbers as reals: x > 50 OR x <= 50 must stay complementary
        # Read as "other > n" (GT with the literal right, or LT with it left) or "other < n".
        greater = isinstance(node, exp.GT) == literal_on_right
        bound = _number(value + 1 if greater else value - 1)
        if literal_on_right:
            return (exp.GTE if greater else exp.LTE)(this=other.copy(), expression=bound)
        return (exp.LTE if greater else exp.GTE)(this=bound, expression=other.copy())
    return None


# Dialects where ``/`` is never integer division and AVG of integers is not truncated.
_EXACT_DIVISION = {"mysql", "bigquery", "duckdb", "spark", "databricks", "snowflake"}
_NUMERIC = (exp.Sum, exp.Avg, exp.Count, exp.Add, exp.Sub, exp.Mul, exp.Div, exp.Round, exp.Abs)


def _times_one(node: exp.Mul) -> exp.Expression | None:
    for one, other in ((node.this, node.expression), (node.expression, node.this)):
        if not (isinstance(one, exp.Literal) and not one.is_string and re.fullmatch(r"1(\.0*)?", one.name or "")):
            continue
        value = other.unnest() if isinstance(other, exp.Paren) else other
        parent = node.parent
        while isinstance(parent, exp.Paren):
            parent = parent.parent
        numeric_context = isinstance(parent, (exp.Add, exp.Sub, exp.Mul, exp.Div)) or (
            isinstance(parent, (exp.Sum, exp.Avg)) and not parent.args.get("distinct") and not isinstance(parent.parent, exp.Window)
        )
        if isinstance(value, _NUMERIC) or (isinstance(value, exp.Literal) and not value.is_string) or numeric_context:
            return other.copy()
    return None


def fold_casts_and_constant_cases(select: exp.Select, types: dict, dialect: str = "bigquery") -> exp.Expression | None:
    """Apply the identities above to ``select``'s own expressions (not nested selects)."""

    def own(node: exp.Expression) -> bool:
        return node.find_ancestor(exp.Select) is select

    changed = False
    for node in list(select.find_all(exp.Is)):
        if own(node):
            folded = _constant_is_null(node)
            if folded is not None:
                node.replace(folded)
                changed = True
    for node in list(select.find_all(exp.GT, exp.LT)):
        if own(node):
            folded = _non_strict(node, select, types)
            if folded is not None:
                node.replace(folded)
                changed = True
    order = select.args.get("order")
    for ordered in order.expressions if order is not None else []:
        key = ordered.this.unnest() if isinstance(ordered.this, exp.Paren) else ordered.this
        inner = _exact_numeric_cast(key, select, types, dialect)
        if inner is not None:
            ordered.set("this", inner.copy())
            changed = True
    for node in list(select.find_all(exp.Is)):
        if own(node) and isinstance(node.expression, exp.Null):
            inner = _exact_numeric_cast(node.this.unnest() if isinstance(node.this, exp.Paren) else node.this, select, types, dialect)
            if inner is not None:
                node.set("this", inner.copy())
                changed = True
    for node in list(select.find_all(exp.Round)):
        if own(node) and node.args.get("decimals") is None and isinstance(node.this, exp.Expression):
            node.set("decimals", exp.Literal.number(0))
            changed = True
    if dialect in _EXACT_DIVISION:
        for node in [m for m in select.find_all(exp.Mul) if own(m)][::-1]:
            if node.parent is None:
                continue
            folded = _times_one(node)
            if folded is not None:
                node.replace(folded)
                changed = True
    for node in list(select.find_all(exp.Not)):
        if own(node) and isinstance(node.this, exp.Boolean):
            node.replace(exp.Boolean(this=not node.this.this))
            changed = True
    for node in list(select.find_all(exp.Case)):
        if own(node) and node.parent is not None:
            folded = _fold_case(node)
            if folded is not None:
                node.replace(folded)
                changed = True
    for cast in [c for c in select.find_all(exp.Cast) if own(c)][::-1]:
        if cast.parent is None:
            continue
        folded = _fold_cast(cast, select, types, dialect)
        if folded is not None:
            cast.replace(folded)
            changed = True
    return select if changed else None
