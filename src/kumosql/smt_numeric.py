"""Scaled NUMERIC and BIGNUMERIC arithmetic for the SMT prover (BigQuery dialect).

The prover keeps every number as one exact rational. That is right for ``+`` and ``-`` of NUMERIC values (nine
decimal digits stay nine), but a product, a quotient, a ``CAST`` and ``ROUND`` can leave the scale: BigQuery rounds the
result back to nine decimal digits (NUMERIC) or 38 (BIGNUMERIC). This module compiles those operations over **values of
a known scale** to the exact rounded rational, so ``n * m`` is the rounded product, ``ROUND(n * m, 9)`` is the same
number, ``NUMERIC '1.50'`` and ``NUMERIC '1.5'`` are one value and ``n / 3 * 3 = n`` is refuted.

A value has a known scale when its type is visible and its digits are constrained: a column declared INT64, NUMERIC or
BIGNUMERIC (``typed_fact`` states the scale and the range of its values), an integer literal, a string literal cast to
NUMERIC or BIGNUMERIC, and an operation of this module on such values. Anything else (an aggregate, a ``CASE``, a
FLOAT64, a value of unknown type) is not compiled here and stays what the prover already made of it (an uninterpreted
function): where the scale cannot be bounded the answer is unknown, never a guess.

The rules, from the GoogleSQL documentation as far as the repository's own pages go, and *unverified* where they do not:

* NUMERIC is nine decimal digits and 29 integer digits, BIGNUMERIC 38 decimal digits (``smt_values``); a result outside
  the range is a runtime error, recorded as an error site (so a rewrite that moves an overflow is classified).
* ``+`` and ``-`` of exact values are exact. ``*`` and ``/`` round the exact result to the scale of the result type,
  half away from zero (*unverified* for ``*`` and ``/``; the repository's pages state it for conversions to NUMERIC and
  for ``ROUND``). INT64 operands convert to the other operand's type without rounding, so a product with an INT64 factor
  keeps its scale and needs no rounding.
* ``CAST`` to NUMERIC or BIGNUMERIC of an INT64, NUMERIC or BIGNUMERIC value, and of a string literal of plain decimal
  digits (``'1.50'``, ``'-0.5'``; no exponent, no spaces, no ``.5``), which rounds half away from zero past the scale
  (*unverified* that a ``NUMERIC '..'`` literal and ``CAST(.. AS NUMERIC)`` round alike; the fixture marks it).
  A cast from FLOAT64 is not compiled (the double's exact binary value is not what the prover keeps).
* ``ROUND(x, k)`` of a NUMERIC or BIGNUMERIC value with an integer literal ``k`` rounds half away from zero to ``k``
  decimal digits (a negative ``k`` to a power of ten) and keeps the type. A ``k`` beyond +-40 is not compiled.
* The upper bound of BIGNUMERIC is *unverified* at the last digit (``smt_values.BIGNUMERIC_LIMIT``).
"""

from __future__ import annotations

from fractions import Fraction
import re

from . import smt_values as S

try:  # pragma: no cover
    import z3
except ImportError:  # pragma: no cover
    z3 = None

SCALE = {S.INT64: 0, S.NUMERIC: S.NUMERIC_SCALE, S.BIGNUMERIC: S.BIGNUMERIC_SCALE}
_PLAIN_DECIMAL = re.compile(r"[+-]?\d+(\.\d+)?")
_MAX_TEXT = 80
_MAX_PLACES = 40


def _real(value: Fraction):
    return z3.RealVal(f"{value.numerator}/{value.denominator}")


def round_half_away(value, places: int):
    """``value`` (a z3 Real) rounded to ``places`` decimal digits (negative: a power of ten), half away from zero."""

    factor = Fraction(10) ** places
    scaled = value * _real(factor)
    half = z3.RealVal("1/2")
    whole = z3.If(scaled >= 0, z3.ToInt(scaled + half), -z3.ToInt(-scaled + half))
    return z3.ToReal(whole) / _real(factor)


def out_of_range(cls: str, value):
    """The condition that a result ``value`` of type ``cls`` is outside the type's range (an overflow error)."""

    if cls == S.NUMERIC:
        limit = _real(Fraction(10 ** (S.NUMERIC_DIGITS - S.NUMERIC_SCALE)))
        return z3.Or(value >= limit, value <= -limit)
    limit = _real(Fraction(S.BIGNUMERIC_LIMIT, 10**S.BIGNUMERIC_SCALE))
    return z3.Or(value >= limit, value < -limit)


def typed_fact(cls: str, V, v):
    """What the declared type ``cls`` (NUMERIC or BIGNUMERIC) says about the value of a column: a number with at most
    ``SCALE`` decimal digits, inside the range."""

    x = V.num(v.val)
    scaled = x * 10 ** SCALE[cls]
    if cls == S.NUMERIC:
        limit = 10 ** (S.NUMERIC_DIGITS - S.NUMERIC_SCALE)
        return z3.And(V.is_Num(v.val), z3.IsInt(scaled), x < limit, x > -limit)
    return z3.And(V.is_Num(v.val), z3.IsInt(scaled), scaled >= -S.BIGNUMERIC_LIMIT, scaled < S.BIGNUMERIC_LIMIT)


# --- which expressions are compiled here ---------------------------------------------------------


def _literal_places(node):
    """The integer literal ``k`` of ``ROUND(x, k)`` (``None`` when it is anything else)."""

    from sqlglot import exp

    if node is None:
        return 0
    negate = isinstance(node, exp.Neg)
    inner = node.this if negate else node
    if isinstance(inner, exp.Literal) and not inner.is_string and S.is_integer_text(inner.this) and len(inner.this) <= 3:
        places = int(inner.this)
        return -places if negate else places
    return None


def _wide(a: str, b: str) -> str:
    return S.BIGNUMERIC if S.BIGNUMERIC in (a, b) else S.NUMERIC if S.NUMERIC in (a, b) else S.INT64


def exact_class(compiler, e, env, agg, aliases) -> str | None:
    """INT64, NUMERIC or BIGNUMERIC when ``e`` is a value of known scale (see the module docstring), else ``None``.
    Compiles nothing: it only reads the tree and the declared column types."""

    from sqlglot import exp

    if isinstance(e, exp.Paren):
        return exact_class(compiler, e.this, env, agg, aliases)
    if isinstance(e, exp.Literal):
        return S.INT64 if not e.is_string and S.is_integer_text(e.this) else None
    if isinstance(e, exp.Neg):
        if isinstance(e.this, exp.Literal):
            return exact_class(compiler, e.this, env, agg, aliases)
        return S.NUMERIC if exact_class(compiler, e.this, env, agg, aliases) == S.NUMERIC else None
    if isinstance(e, exp.Column):
        cls = compiler._class_of(e, env, agg, aliases)
        return cls if cls in SCALE else None
    if isinstance(e, (exp.Add, exp.Sub, exp.Mul, exp.Div)):
        typed = S.fold_typed(e)
        if typed is not None:
            return S.INT64 if typed[0] == "int" else None  # literal arithmetic is folded as BigQuery evaluates it
        left = exact_class(compiler, e.this, env, agg, aliases)
        right = exact_class(compiler, e.expression, env, agg, aliases)
        if left is None or right is None:
            return None
        wide = _wide(left, right)
        return None if wide == S.INT64 else wide  # INT64 op INT64 is not modelled here (and ``/`` of two is FLOAT64)
    if isinstance(e, exp.Cast) and not isinstance(e, exp.TryCast):
        to = e.args["to"]
        if to.args.get("expressions"):
            return None  # NUMERIC(p, s): BigQuery rejects it, the prover declines it elsewhere
        target = S.type_class(to.sql(dialect="bigquery"))
        if target not in (S.NUMERIC, S.BIGNUMERIC):
            return None
        inner = e.this.unnest() if isinstance(e.this, exp.Paren) else e.this
        if isinstance(inner, exp.Literal) and inner.is_string:
            return target if literal_decimal(inner.this, target) is not None else None
        return target if exact_class(compiler, inner, env, agg, aliases) is not None else None
    if isinstance(e, exp.Round) and not e.args.get("truncate") and not e.args.get("casing_mode"):
        places = _literal_places(e.args.get("decimals"))
        if places is None or abs(places) > _MAX_PLACES:
            return None
        cls = exact_class(compiler, e.this, env, agg, aliases)
        return cls if cls in (S.NUMERIC, S.BIGNUMERIC) else None
    return None


def literal_decimal(text: str, target: str) -> Fraction | None:
    """The NUMERIC or BIGNUMERIC value of the string literal ``text`` (plain decimal digits only), or ``None``."""

    if len(text) > _MAX_TEXT or not _PLAIN_DECIMAL.fullmatch(text):
        return None
    try:
        return S.numeric_from(text) if target == S.NUMERIC else S.bignumeric_from(text)
    except S.BigQueryError:
        return None


# --- compiling them ------------------------------------------------------------------------------


def compile_node(compiler, e, env, agg, aliases):
    """``(null, value)`` of ``e`` when it is an operation of this module on values of known scale, else ``None``.
    Records the overflow and division errors it can raise as error sites of the compiler."""

    built = _build(compiler, e, env, agg, aliases)
    return None if built is None else built[:2]


def _build(compiler, e, env, agg, aliases):
    """``(null, value, digits)``: ``digits`` bounds the decimal digits the value really has (at most its type's scale),
    so a rounding to that many digits or more changes nothing and is left out of the formula."""

    from sqlglot import exp

    if not isinstance(e, (exp.Add, exp.Sub, exp.Mul, exp.Div, exp.Neg, exp.Cast, exp.Round)) or isinstance(e, exp.TryCast):
        return None
    cls = exact_class(compiler, e, env, agg, aliases)
    if cls not in (S.NUMERIC, S.BIGNUMERIC):
        return None
    from .smt_equivalence import _value_sort

    return _compile(compiler, _value_sort(), e, cls, env, agg, aliases)


def _operand(compiler, V, e, env, agg, aliases):
    """``(null, real value, digits)`` of an operand already known to have a scale."""

    e = e.unnest() if hasattr(e, "unnest") else e
    built = _build(compiler, e, env, agg, aliases)
    if built is not None:
        return built
    cls = exact_class(compiler, e, env, agg, aliases)
    v = compiler._val(e, env, agg, aliases)
    compiler._numeric(v)
    return v.null, v.val, SCALE[cls]


def _overflow(compiler, V, e, cls, nulls, value) -> None:
    compiler._site(f"{cls} overflow", e, z3.And(*[z3.Not(n) for n in nulls], out_of_range(cls, value)))


def _digits(value: Fraction) -> int:
    places = 0
    while (value * 10**places).denominator != 1:
        places += 1
    return places


def _compile(compiler, V, e, cls, env, agg, aliases):
    from sqlglot import exp

    scale = SCALE[cls]
    if isinstance(e, exp.Cast):
        inner = e.this.unnest() if isinstance(e.this, exp.Paren) else e.this
        if isinstance(inner, exp.Literal) and inner.is_string:
            value = literal_decimal(inner.this, cls)
            return z3.BoolVal(False), V.Num(_real(value)), _digits(value)
        null, value, digits = _operand(compiler, V, inner, env, agg, aliases)
        if digits <= scale:
            return null, value, digits  # every value fits: INT64, or a decimal with no more digits than the target keeps
        rounded = round_half_away(V.num(value), scale)
        _overflow(compiler, V, e, cls, [null], rounded)
        return null, V.Num(rounded), scale
    if isinstance(e, exp.Round):
        null, value, digits = _operand(compiler, V, e.this, env, agg, aliases)
        places = _literal_places(e.args.get("decimals"))
        if places >= digits:
            return null, value, digits  # nothing to round off
        rounded = round_half_away(V.num(value), places)
        _overflow(compiler, V, e, cls, [null], rounded)
        return null, V.Num(rounded), max(places, 0)
    if isinstance(e, exp.Neg):
        null, value, digits = _operand(compiler, V, e.this, env, agg, aliases)
        return null, V.Num(-V.num(value)), digits
    left_null, left, left_digits = _operand(compiler, V, e.this, env, agg, aliases)
    right_null, right, right_digits = _operand(compiler, V, e.expression, env, agg, aliases)
    x, y = V.num(left), V.num(right)
    nulls = [left_null, right_null]
    if isinstance(e, (exp.Add, exp.Sub)):
        result = x + y if isinstance(e, exp.Add) else x - y
        digits = max(left_digits, right_digits)
    elif isinstance(e, exp.Mul):
        result, digits = x * y, left_digits + right_digits
        if digits > scale:
            result, digits = round_half_away(result, scale), scale
    else:
        from .smt_equivalence import _Val

        compiler._division_site(e, _Val(left_null, left), _Val(right_null, right), False)
        result, digits = round_half_away(x / y, scale), scale
    _overflow(compiler, V, e, cls, nulls, result)
    return z3.Or(*nulls), V.Num(result), digits
