"""BigQuery's numeric values for the SMT prover: types, literals, and the reference arithmetic.

The prover models every number as one exact rational (``smt_equivalence``). This module keeps that
model honest about what BigQuery really computes, in three parts:

* **Types** (:func:`type_class`): INT64, NUMERIC, BIGNUMERIC and FLOAT64 from a declared column type, so
  the compiler can tell an integer column (bounded, integral, never NaN) from a floating one.
* **Literals** (:func:`literal_value`, :func:`fold_literals`): a BigQuery decimal or exponent literal is
  a FLOAT64, so it stands for the double nearest its text (``1e-324`` and ``2e-324`` are both zero, and
  ``0.1`` and ``0.10000000000000001`` are the same number); an integer literal is an INT64 and is range
  checked. Arithmetic over literals is folded the way BigQuery evaluates it (INT64 exact, FLOAT64 as an
  IEEE double, ``/`` always FLOAT64), so ``0.1 + 0.2 = 0.3`` is FALSE.
* **Reference arithmetic** (``int64_*``, ``float64_*``, ``numeric_*``, :func:`div`, :func:`safe_divide`,
  :func:`ieee_divide`): the value or the error BigQuery returns for one operation on concrete values.
  The prover calls them only to fold literal-only arithmetic; tests and the numeric-traps eval use them to
  check what each label says against the documented rules.

What the rules are, from the GoogleSQL documentation (data types, operators, mathematical functions,
conversion rules); anything the author could not confirm there is marked *unverified*:

* INT64 ``+ - *`` and unary ``-`` raise an error on overflow; ``ABS(INT64_MIN)`` and ``DIV(INT64_MIN, -1)``
  overflow too. ``DIV`` truncates toward zero. ``/`` of two INT64 is FLOAT64 (both converted first).
* ``/`` and ``MOD`` raise an error for a zero divisor, FLOAT64 included; ``IEEE_DIVIDE`` returns +-inf or NaN
  instead and ``SAFE_DIVIDE`` returns NULL (also when the quotient overflows).
* FLOAT64 arithmetic that overflows raises an error rather than returning infinity (unverified for every
  operator; the GoogleSQL mathematical-functions page states it for ``EXP`` and ``POW``).
* NUMERIC is 29 integer digits and 9 decimal digits; BIGNUMERIC is a 255-bit scaled integer with 38 decimal
  digits (unverified: the exact upper bound). Conversions to NUMERIC round half away from zero.
* ``CAST(FLOAT64 AS INT64)`` rounds half away from zero and raises an error for NaN, infinity and values out
  of range.
* NaN is not equal to anything, itself included; ``GROUP BY``, ``DISTINCT`` and ``PARTITION BY`` put all NaNs
  in one group and treat ``-0.0`` and ``0.0`` as one value; ``ORDER BY`` puts NaN before every other number.
* An INT64 compared with, or combined by CASE, IF, COALESCE or a set operation with a FLOAT64, is converted
  to FLOAT64 first, which rounds values past 2**53.
"""

from __future__ import annotations

from decimal import Decimal, ROUND_HALF_UP, localcontext
from fractions import Fraction
import math
import re

INT64_MIN = -(2**63)
INT64_MAX = 2**63 - 1
FLOAT_EXACT_INT = 2**53  # INT64 values up to here have an exact FLOAT64
NUMERIC_SCALE = 9
NUMERIC_DIGITS = 38  # total decimal digits: 29 before the point and 9 after
BIGNUMERIC_SCALE = 38
BIGNUMERIC_LIMIT = 2**255  # scaled-integer bound (unverified at the last digit)


class BigQueryError(Exception):
    """BigQuery raises a runtime error for this operation on these values."""


class NotModeled(Exception):
    """The prover has no faithful value for this literal or operation."""


# --- types ------------------------------------------------------------------------------------

INT64, NUMERIC, BIGNUMERIC, FLOAT64, STRING, BOOL, OTHER = "INT64", "NUMERIC", "BIGNUMERIC", "FLOAT64", "STRING", "BOOL", "OTHER"

_TYPE_CLASSES = {
    **dict.fromkeys(("INT64", "INT", "SMALLINT", "INTEGER", "BIGINT", "TINYINT", "BYTEINT"), INT64),
    **dict.fromkeys(("NUMERIC", "DECIMAL"), NUMERIC),
    **dict.fromkeys(("BIGNUMERIC", "BIGDECIMAL"), BIGNUMERIC),
    **dict.fromkeys(("FLOAT64", "FLOAT", "FLOAT32", "FLOAT4", "FLOAT8", "DOUBLE", "DOUBLE PRECISION", "REAL"), FLOAT64),
    **dict.fromkeys(("STRING", "VARCHAR", "TEXT"), STRING),
    **dict.fromkeys(("BOOL", "BOOLEAN"), BOOL),
    **dict.fromkeys(("BYTES", "DATE", "DATETIME", "TIME", "TIMESTAMP", "JSON", "GEOGRAPHY"), OTHER),
}


def type_class(declared: str | None) -> str | None:
    """INT64, NUMERIC, BIGNUMERIC or FLOAT64 for a declared column type, ``STRING`` or ``BOOL``, ``OTHER`` for another
    known type (DATE, BYTES, ..), ``None`` when the type is absent or not recognised."""

    if not declared:
        return None
    name = re.sub(r"[(<].*", "", declared).strip().upper()
    return _TYPE_CLASSES.get(name)


def arithmetic_class(a: str | None, b: str | None) -> str | None:
    """The type of ``a + b``, ``a - b`` and ``a * b`` for operand types ``a`` and ``b`` (``None`` when either is unknown)."""

    if a is None or b is None or {a, b} & {OTHER, STRING, BOOL, "NULL"}:
        return None
    for wide in (FLOAT64, BIGNUMERIC, NUMERIC):
        if wide in (a, b):
            return wide
    return INT64


def division_class(a: str | None, b: str | None) -> str | None:
    """The type of ``a / b``: FLOAT64 for two INT64s, as for any FLOAT64 operand; NUMERIC or BIGNUMERIC when one is."""

    wide = arithmetic_class(a, b)
    return FLOAT64 if wide == INT64 else wide


def supertype(classes: list) -> str | None:
    """The common type of the branches of a CASE, IF or COALESCE (``None`` when one is unknown); NULL literals are skipped."""

    classes = [c for c in classes if c != "NULL"]
    if not classes or any(c is None for c in classes):
        return None
    if len(set(classes)) == 1:
        return classes[0]
    return arithmetic_class(classes[0], supertype(classes[1:]) if len(classes) > 2 else classes[1])


# Functions that never produce a FLOAT64 from integer, string and boolean arguments.
_INTEGER_SAFE_FUNCTIONS = (
    "Count", "CountIf", "Sum", "Min", "Max", "Abs", "Coalesce", "If", "Case", "Nullif", "Greatest", "Least", "Mod", "IntDiv",
    "Length", "Upper", "Lower", "Concat", "DPipe", "Trim", "Cast", "TryCast", "Substring", "Left", "Right", "Distinct",
    "Max", "Min", "Between", "In", "Is", "Like", "ILike", "Not", "And", "Or", "Paren", "Alias", "Ordered", "Neg",
)


def float_capable(tree) -> bool:
    """Whether ``tree`` can produce a FLOAT64: a decimal or exponent literal, a division, an average, a cast to a
    floating or decimal type, or a function this module does not know to keep integers integral."""

    from sqlglot import exp

    for node in tree.walk():
        if isinstance(node, exp.Literal) and not node.is_string and not is_integer_text(node.this):
            return True
        if isinstance(node, (exp.Div, exp.Avg)):
            return True
        if isinstance(node, (exp.Cast, exp.TryCast)):
            if type_class(node.args["to"].sql(dialect="bigquery")) not in (INT64, STRING, BOOL, OTHER):
                return True
        elif isinstance(node, exp.Func) and type(node).__name__ not in _INTEGER_SAFE_FUNCTIONS and not isinstance(node, exp.Column):
            return True
    return False


# Casts that cannot fail: (source type, target type name).
_SAFE_CASTS = {
    (INT64, "INT64"), (INT64, "INT"), (INT64, "BIGINT"), (INT64, "FLOAT64"), (INT64, "FLOAT"), (INT64, "NUMERIC"), (INT64, "BIGNUMERIC"),
    (INT64, "STRING"), (INT64, "BOOL"), (INT64, "BOOLEAN"),
    (NUMERIC, "NUMERIC"), (NUMERIC, "BIGNUMERIC"), (NUMERIC, "FLOAT64"), (NUMERIC, "FLOAT"), (NUMERIC, "STRING"),
    (BIGNUMERIC, "BIGNUMERIC"), (BIGNUMERIC, "STRING"),
    (FLOAT64, "FLOAT64"), (FLOAT64, "FLOAT"), (FLOAT64, "STRING"),
    (BOOL, "BOOL"), (BOOL, "BOOLEAN"), (BOOL, "INT64"), (BOOL, "INT"), (BOOL, "BIGINT"), (BOOL, "STRING"),
    (STRING, "STRING"),
}


def cast_is_safe(source: str, target: str) -> bool:
    """Whether ``CAST(source AS target)`` can never raise an error (``source`` a type class, ``target`` a type name)."""

    return (source, target) in _SAFE_CASTS


_DATE_TEXT = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def string_cast_ok(text: str, target: str) -> bool:
    """Whether casting the string literal ``text`` to ``target`` certainly succeeds (conservative: ``False`` is not an error)."""

    if target in ("INT64", "INT", "BIGINT"):
        return bool(re.fullmatch(r"[+-]?\d{1,18}", text))
    if target in ("FLOAT64", "FLOAT"):
        return bool(re.fullmatch(r"[+-]?(\d+(\.\d*)?|\.\d+)([eE][+-]?\d{1,2})?", text))
    if target in ("BOOL", "BOOLEAN"):
        return text.lower() in ("true", "false")
    if target == "DATE" and _DATE_TEXT.match(text):
        import datetime

        try:
            datetime.date.fromisoformat(text)
        except ValueError:
            return False
        return True
    return False


# --- literals ---------------------------------------------------------------------------------

_INTEGER_TEXT = re.compile(r"^\d+$")


def is_integer_text(text: str) -> bool:
    return bool(_INTEGER_TEXT.match(text))


def float64(value) -> float:
    """The FLOAT64 nearest ``value`` (an int, Fraction, Decimal or text); ties to even, as the hardware rounds."""

    if isinstance(value, str):
        return float(value)
    if isinstance(value, Decimal):
        return float(value)
    return float(Fraction(value))


def float_literal(text: str, negate: bool = False) -> Fraction:
    """The exact value of the FLOAT64 that the decimal or exponent literal ``text`` denotes.

    Raises :class:`NotModeled` when the literal overflows FLOAT64, which BigQuery reads as an infinity or
    rejects (unverified which), and neither fits the prover's rational values."""

    exponent = re.search(r"[eE]([+-]?\d+)$", text)
    if exponent and abs(int(exponent.group(1))) > 400:
        raise NotModeled(f"numeric literal {text[:40]!r} has an exponent far outside FLOAT64")
    try:
        number = float(text)
    except (ValueError, OverflowError) as error:
        raise NotModeled(f"numeric literal {text[:40]!r}") from error
    if math.isinf(number) or math.isnan(number):
        raise NotModeled(f"numeric literal {text[:40]!r} overflows FLOAT64")
    value = Fraction(number)
    return -value if negate else value


def integer_literal(text: str, negate: bool = False) -> Fraction:
    """The INT64 value of an integer literal; ``-9223372036854775808`` is the one literal that is negative only
    after its sign is applied."""

    value = int(text)
    if negate:
        value = -value
    if not INT64_MIN <= value <= INT64_MAX:
        raise NotModeled(f"integer literal {text[:40]} is outside INT64")
    return Fraction(value)


def literal_value(text: str, negate: bool = False) -> Fraction:
    """The value a BigQuery numeric literal stands for: an INT64 when it is digits only, else a FLOAT64."""

    if len(text) > 400:
        raise NotModeled("a numeric literal longer than 400 characters")
    return integer_literal(text, negate) if is_integer_text(text) else float_literal(text, negate)


def is_negative_zero_literal(text: str, negate: bool) -> bool:
    """``-0.0`` and friends: a FLOAT64 literal that is zero under a minus sign."""

    return negate and not is_integer_text(text) and float_literal(text) == 0


# --- folding arithmetic over literals ---------------------------------------------------------

_INT, _FLOAT = "int", "float"


def _typed(node):
    """``(kind, value)`` of ``+ - * /`` and parentheses over numeric literals, evaluated as BigQuery does (an INT64 is
    a Python int, a FLOAT64 a Python float), or ``None`` when the expression is not only literals or BigQuery would
    raise an error on it."""

    from sqlglot import exp

    if isinstance(node, exp.Paren):
        return _typed(node.this)
    if isinstance(node, exp.Literal):
        return _leaf(node, False)
    if isinstance(node, exp.Neg):
        inner = node.this
        if isinstance(inner, exp.Literal):
            return _leaf(inner, True)
        value = _typed(inner)
        if value is None or (value[0] == _FLOAT and value[1] == 0):
            return None  # -0.0 is not a number the model tells apart from 0.0
        try:
            return (_INT, int64_neg(value[1])) if value[0] == _INT else (_FLOAT, -value[1])
        except BigQueryError:
            return None
    if isinstance(node, (exp.Add, exp.Sub, exp.Mul, exp.Div)):
        left, right = _typed(node.this), _typed(node.expression)
        if left is None or right is None:
            return None
        try:
            if isinstance(node, exp.Div):
                return (_FLOAT, div(_as_float(left), _as_float(right)))
            if left[0] == _INT and right[0] == _INT:
                op = int64_add if isinstance(node, exp.Add) else int64_sub if isinstance(node, exp.Sub) else int64_mul
                return (_INT, op(left[1], right[1]))
            op = float64_add if isinstance(node, exp.Add) else float64_sub if isinstance(node, exp.Sub) else float64_mul
            return (_FLOAT, op(_as_float(left), _as_float(right)))
        except BigQueryError:
            return None
    return None


def _leaf(node, negate: bool):
    if node.is_string:
        return None
    text = node.this
    try:
        if is_integer_text(text):
            return (_INT, int(integer_literal(text, negate)))
        value = float(float_literal(text, negate))
    except NotModeled:
        return None
    return None if value == 0 and negate else (_FLOAT, value)


def _as_float(typed) -> float:
    kind, value = typed
    return float64(value)


def fold_typed(node):
    """``("int" | "float", exact value)`` of a literal-only ``+ - * /`` expression, or ``None``."""

    from sqlglot import exp

    if not isinstance(node, (exp.Literal, exp.Paren, exp.Add, exp.Sub, exp.Mul, exp.Div, exp.Neg)):
        return None
    typed = _typed(node)
    return None if typed is None else (typed[0], Fraction(typed[1]))


def fold_literals(node) -> Fraction | None:
    """The exact value of a literal-only ``+ - * /`` expression, or ``None``."""

    typed = fold_typed(node)
    return None if typed is None else typed[1]


# --- INT64 ------------------------------------------------------------------------------------


def int64_check(value: int) -> int:
    if not INT64_MIN <= value <= INT64_MAX:
        raise BigQueryError("INT64 overflow")
    return value


def int64_add(a: int, b: int) -> int:
    return int64_check(a + b)


def int64_sub(a: int, b: int) -> int:
    return int64_check(a - b)


def int64_mul(a: int, b: int) -> int:
    return int64_check(a * b)


def int64_neg(a: int) -> int:
    return int64_check(-a)


def int64_abs(a: int) -> int:
    return int64_check(abs(a))


def int64_div(a: int, b: int) -> int:
    """``DIV(a, b)``: truncation toward zero."""

    if b == 0:
        raise BigQueryError("division by zero")
    quotient = abs(a) // abs(b)
    return int64_check(quotient if (a < 0) == (b < 0) else -quotient)


def int64_mod(a: int, b: int) -> int:
    """``MOD(a, b)``: the remainder has the sign of the dividend."""

    if b == 0:
        raise BigQueryError("division by zero")
    return int64_check(abs(a) % abs(b) * (1 if a >= 0 else -1))


# --- FLOAT64 ----------------------------------------------------------------------------------


def _finite(value: float) -> float:
    if math.isinf(value):
        raise BigQueryError("floating point overflow")
    return value


def float64_add(a: float, b: float) -> float:
    return _finite(a + b) if math.isfinite(a) and math.isfinite(b) else a + b


def float64_sub(a: float, b: float) -> float:
    return _finite(a - b) if math.isfinite(a) and math.isfinite(b) else a - b


def float64_mul(a: float, b: float) -> float:
    return _finite(a * b) if math.isfinite(a) and math.isfinite(b) else a * b


def div(a: float, b: float) -> float:
    """``a / b`` over FLOAT64 (INT64 operands are converted first): zero divisor and overflow are errors."""

    if b == 0:
        raise BigQueryError("division by zero")
    if math.isnan(a) or math.isnan(b):
        return math.nan
    if math.isinf(a) or math.isinf(b):
        return a / b
    return _finite(a / b)


def ieee_divide(a: float, b: float) -> float:
    """``IEEE_DIVIDE``: the IEEE quotient, infinities and NaN included."""

    if math.isnan(a) or math.isnan(b):
        return math.nan
    if b == 0:
        if a == 0:
            return math.nan
        return math.copysign(math.inf, a) * math.copysign(1.0, b)
    return a / b


def safe_divide(a: float, b: float) -> float | None:
    """``SAFE_DIVIDE``: NULL (``None``) where ``/`` would raise an error."""

    try:
        return div(a, b)
    except BigQueryError:
        return None


def float64_to_int64(value: float) -> int:
    """``CAST(value AS INT64)``: round half away from zero; NaN, infinity and out-of-range values are errors."""

    if math.isnan(value) or math.isinf(value):
        raise BigQueryError("cast of a non-finite FLOAT64 to INT64")
    rounded = int(Decimal(value).to_integral_value(rounding=ROUND_HALF_UP))
    return int64_check(rounded)


def float_key(value: float):
    """What ``GROUP BY`` and ``DISTINCT`` compare: all NaNs alike, ``-0.0`` and ``0.0`` alike."""

    if math.isnan(value):
        return "nan"
    return 0.0 if value == 0 else value


def float_order_key(value: float):
    """``ORDER BY`` ascending: NaN before every number."""

    return (0, 0.0) if math.isnan(value) else (1, value)


def float_equal(a: float, b: float) -> bool:
    return a == b  # IEEE: NaN is unequal to itself, -0.0 equals 0.0


# --- NUMERIC and BIGNUMERIC ---------------------------------------------------------------------


def _scaled(value, scale: int) -> int:
    """``value`` (an int, Fraction, Decimal or a double's exact value) times ``10**scale``, rounded half away from zero."""

    exact = Fraction(value) * 10**scale
    rounded = math.floor(abs(exact) + Fraction(1, 2))
    return rounded if exact >= 0 else -rounded


def numeric_from(value) -> Fraction:
    """A value as NUMERIC: nine decimal digits, rounded half away from zero; an error out of range.

    A FLOAT64 converts from its exact binary value (``CAST(0.0000000005 AS NUMERIC)`` is not the same as
    ``NUMERIC '0.0000000005'``), a string from its decimal text."""

    if isinstance(value, float):
        if not math.isfinite(value):
            raise BigQueryError("cast of a non-finite FLOAT64 to NUMERIC")
        value = Fraction(value)
    elif isinstance(value, str):
        value = Fraction(Decimal(value.strip()))
    scaled = _scaled(value, NUMERIC_SCALE)
    if abs(scaled) >= 10**NUMERIC_DIGITS:
        raise BigQueryError("NUMERIC out of range")
    return Fraction(scaled, 10**NUMERIC_SCALE)


def bignumeric_from(value) -> Fraction:
    """A value as BIGNUMERIC: 38 decimal digits, rounded half away from zero (the range is unverified at the last digit)."""

    if isinstance(value, float):
        if not math.isfinite(value):
            raise BigQueryError("cast of a non-finite FLOAT64 to BIGNUMERIC")
        value = Fraction(value)
    elif isinstance(value, str):
        value = Fraction(Decimal(value.strip()))
    scaled = _scaled(value, BIGNUMERIC_SCALE)
    if not -BIGNUMERIC_LIMIT <= scaled < BIGNUMERIC_LIMIT:
        raise BigQueryError("BIGNUMERIC out of range")
    return Fraction(scaled, 10**BIGNUMERIC_SCALE)


def numeric_text(value: Fraction) -> str:
    """The canonical decimal text of a NUMERIC value: no exponent, no trailing zeros, no ``+``."""

    with localcontext() as context:
        context.prec = 80
        text = format(Decimal(value.numerator) / Decimal(value.denominator), "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return "0" if text in ("-0", "") else text
