"""Function family: math (rounding, powers and logarithms, trigonometry, GREATEST/LEAST, bit counts, hashes).

FLOAT64 results of libm functions (exp, ln, pow, trigonometry...) may differ from BigQuery's in the last bit, so those
handlers set ``ctx.inexact``. Where BigQuery's rule for a NUMERIC/BIGNUMERIC version of a function is not known exactly
(SQRT, EXP, LN, LOG, POW), that input type raises ``Unsupported``.
"""

from __future__ import annotations

import hashlib
import math
from decimal import (
    ROUND_CEILING,
    ROUND_DOWN,
    ROUND_FLOOR,
    ROUND_HALF_EVEN,
    ROUND_HALF_UP,
    Decimal,
)
from fractions import Fraction

from sqlglot import exp

from . import types as T
from . import values as V
from .errors import AnalysisError, EvalError, Unsupported
from .functions import (
    arity,
    args_of,
    bad_signature,
    coerce_to,
    guard_argument,
    lift1,
    lift2,
    null_of,
    register,
    register_node,
    safe_wrap,
)
from .runtime import E

INF = math.inf
NAN = math.nan


# --- type resolution ---------------------------------------------------------------------------------------------------


def _numeric_args(c, name: str, args: list, kinds=("INT64", "NUMERIC", "BIGNUMERIC", "FLOAT64")) -> tuple[T.Type, list]:
    """The common type of numeric arguments (NULL literals take the others' type) and the arguments coerced to it."""

    for a in args:
        if a.lit != "null" and not a.type.is_numeric:
            raise bad_signature(name, args)
    target = T.supertype([(a.type, a.lit) for a in args]) if args else T.INT64
    if target.kind not in kinds:
        raise bad_signature(name, args)
    return target, [coerce_to(c, name, a, target) for a in args]


def _floats(c, name: str, args: list, exact_types: tuple = ()) -> list:
    """Arguments as FLOAT64 (INT64, NUMERIC and BIGNUMERIC convert); ``Unsupported`` for the NUMERIC kinds in ``exact_types``."""

    out = []
    for a in args:
        if a.lit != "null" and not a.type.is_numeric:
            raise bad_signature(name, args)
        if a.type.kind in exact_types and a.lit != "null":
            raise Unsupported(f"{name} on {a.type.kind}")
        out.append(coerce_to(c, name, a, T.FLOAT64))
    return out


def _finite(x: float) -> bool:
    return not (math.isnan(x) or math.isinf(x))


# --- ABS, SIGN ---------------------------------------------------------------------------------------------------------


@register_node(exp.Abs)
def _map_abs(node):
    return "ABS", args_of(node, "this")


@register("ABS")
def abs_(c, node, args, cx):
    arity("ABS", args, 1)
    a = args[0]
    if a.lit == "null":
        a = c.coerce(a, T.INT64)
    kind = a.type.kind
    if kind == "INT64":
        return lift1(a, T.INT64, lambda v: V.int64(abs(v)))
    if kind == "NUMERIC":
        return lift1(a, a.type, lambda v: V.numeric(abs(v)))
    if kind == "BIGNUMERIC":
        return lift1(a, a.type, lambda v: V.bignumeric(abs(v)))
    if kind == "FLOAT64":
        return lift1(a, a.type, abs)
    raise bad_signature("ABS", args)


@register_node(exp.Sign)
def _map_sign(node):
    return "SIGN", args_of(node, "this")


def _float_sign(x: float) -> float:
    if math.isnan(x):
        return x
    return 1.0 if x > 0 else (-1.0 if x < 0 else x)


@register("SIGN")
def sign(c, node, args, cx):
    arity("SIGN", args, 1)
    a = args[0]
    if a.lit == "null":
        a = c.coerce(a, T.INT64)
    kind = a.type.kind
    if kind == "INT64":
        return lift1(a, T.INT64, lambda v: (v > 0) - (v < 0))
    if kind in ("NUMERIC", "BIGNUMERIC"):
        return lift1(a, a.type, lambda v: Decimal((v > 0) - (v < 0)))
    if kind == "FLOAT64":
        return lift1(a, a.type, _float_sign)
    raise bad_signature("SIGN", args)


# --- CEIL, FLOOR, ROUND, TRUNC -----------------------------------------------------------------------------------------


@register_node(exp.Ceil)
def _map_ceil(node):
    return "CEIL", args_of(node, "this")


@register_node(exp.Floor)
def _map_floor(node):
    return "FLOOR", args_of(node, "this")


@register_node(exp.Round)
def _map_round(node):
    return "ROUND", args_of(node, "this", "decimals", "truncate")


@register_node(exp.Trunc)
def _map_trunc(node):
    return "TRUNC", args_of(node, "this", "decimals")


def _float_integral(x: float, fn) -> float:
    if not _finite(x) or abs(x) >= 2.0**52:
        return x
    return math.copysign(float(fn(x)), x)


def _round_half_away_float(x: float) -> float:
    if not _finite(x) or abs(x) >= 2.0**52:
        return x
    return math.copysign(float(Decimal(x).quantize(Decimal(1), rounding=ROUND_HALF_UP, context=V.DEC)), x)


def _single_float_or_decimal(c, name: str, args: list, float_fn, decimal_rounding):
    """CEIL and FLOOR: FLOAT64 (INT64 converts to it) or an exact decimal rounding for NUMERIC/BIGNUMERIC."""

    arity(name, args, 1)
    a = args[0]
    if a.lit == "null":
        a = c.coerce(a, T.FLOAT64)
    kind = a.type.kind
    if kind in ("NUMERIC", "BIGNUMERIC"):
        convert = V.decimal_of(kind)
        return lift1(a, a.type, lambda v: convert(v.to_integral_value(rounding=decimal_rounding, context=V.DEC)))
    if kind in ("INT64", "FLOAT64"):
        a = coerce_to(c, name, a, T.FLOAT64)
        return lift1(a, T.FLOAT64, float_fn)
    raise bad_signature(name, args)


@register("CEIL", "CEILING")
def ceil(c, node, args, cx):
    return _single_float_or_decimal(c, "CEIL", args, lambda x: _float_integral(x, math.ceil), ROUND_CEILING)


@register("FLOOR")
def floor(c, node, args, cx):
    return _single_float_or_decimal(c, "FLOOR", args, lambda x: _float_integral(x, math.floor), ROUND_FLOOR)


_MODES = {"ROUND_HALF_AWAY_FROM_ZERO": ROUND_HALF_UP, "ROUND_HALF_EVEN": ROUND_HALF_EVEN}


def _decimal_round(convert, rounding, digits: int):
    def run(v: Decimal) -> Decimal:
        if digits >= 38:
            return v
        if digits < -40:
            return Decimal(0)
        return convert(v.quantize(Decimal(1).scaleb(-digits), rounding=rounding, context=V.DEC))

    return run


def _float_round_digits(x: float, digits: int, how) -> float:
    """ROUND/TRUNC of a double to ``digits`` decimal places the way ZetaSQL scales: ``how(x * 10^d) / 10^d``."""

    if not _finite(x):
        return x
    if abs(digits) > 300:
        raise Unsupported("ROUND/TRUNC of a double to more than 300 digits")
    if digits >= 0:
        scale = float(10**digits)
        scaled = x * scale
        if not _finite(scaled):
            return x
        return how(scaled) / scale
    scale = float(10**-digits)
    return how(x / scale) * scale


def _round_or_trunc(c, name: str, args: list, is_round: bool):
    arity(name, args, 1, 3 if is_round else 2)
    value = args[0]
    digits = args[1] if len(args) > 1 else None
    mode_arg = args[2] if len(args) > 2 else None
    rounding_mode = ROUND_HALF_UP
    mode_null = False
    if mode_arg is not None:
        if mode_arg.lit == "null":
            mode_null = True
        elif mode_arg.lit == "literal" and mode_arg.type == T.STRING and mode_arg.value in _MODES:
            rounding_mode = _MODES[mode_arg.value]
        else:
            raise Unsupported("ROUND rounding mode")
    if value.lit == "null":
        value = c.coerce(value, T.FLOAT64)
    kind = value.type.kind
    if kind not in ("INT64", "NUMERIC", "BIGNUMERIC", "FLOAT64"):
        raise bad_signature(name, args)
    if digits is not None:
        digits = coerce_to(c, name, digits, T.INT64)
    if mode_null:
        return null_of(T.FLOAT64 if kind in ("INT64", "FLOAT64") else value.type)
    if kind in ("INT64", "FLOAT64"):
        if is_round and rounding_mode is ROUND_HALF_EVEN:
            raise Unsupported("ROUND_HALF_EVEN on a floating-point value")
        value = coerce_to(c, name, value, T.FLOAT64)
        whole = _round_half_away_float if is_round else (lambda y: _float_integral(y, math.trunc))
        if digits is None:
            return lift1(value, T.FLOAT64, whole)
        vf, df = value.fn, digits.fn

        def run(env):
            x, d = vf(env), df(env)
            if x is None or d is None:
                return None
            if d != 0 and _finite(x):
                env.ctx.inexact = True
            return _float_round_digits(x, d, whole)

        return E(T.FLOAT64, run)
    convert = V.decimal_of(kind)
    rounding = rounding_mode if is_round else ROUND_DOWN
    if digits is None:
        return lift1(value, value.type, _decimal_round(convert, rounding, 0))
    return lift2(value, digits, value.type, lambda v, d: _decimal_round(convert, rounding, d)(v))


@register("ROUND")
def round_(c, node, args, cx):
    return _round_or_trunc(c, "ROUND", args, True)


@register("TRUNC")
def trunc(c, node, args, cx):
    return _round_or_trunc(c, "TRUNC", args, False)


# --- MOD, DIV, IEEE_DIVIDE, SAFE_* -------------------------------------------------------------------------------------


@register_node(exp.Mod)
def _map_mod(node):
    return "MOD", args_of(node, "this", "expression")


@register_node(exp.IntDiv)
def _map_div(node):
    return "DIV", args_of(node, "this", "expression")


def _integer_division(c, name: str, args: list):
    arity(name, args, 2)
    target, (a, b) = _numeric_args(c, name, args, kinds=("INT64", "NUMERIC", "BIGNUMERIC"))
    return target, a, b


@register("MOD")
def mod(c, node, args, cx):
    target, a, b = _integer_division(c, "MOD", args)
    if target.kind == "INT64":

        def int_mod(x, y):
            if y == 0:
                raise EvalError(f"division by zero: MOD({x}, {y})")
            r = abs(x) % abs(y)
            return -r if x < 0 else r

        return lift2(a, b, T.INT64, int_mod)
    convert = V.decimal_of(target.kind)

    def dec_mod(x, y):
        if y == 0:
            raise EvalError(f"division by zero: MOD({V.format_decimal(x)}, {V.format_decimal(y)})")
        return convert(V.DEC.remainder(x, y))

    return lift2(a, b, target, dec_mod)


@register("DIV")
def div(c, node, args, cx):
    target, a, b = _integer_division(c, "DIV", args)
    if target.kind == "INT64":

        def int_div(x, y):
            if y == 0:
                raise EvalError(f"division by zero: DIV({x}, {y})")
            q = abs(x) // abs(y)
            return V.int64(-q if (x < 0) != (y < 0) else q)

        return lift2(a, b, T.INT64, int_div)
    convert = V.decimal_of(target.kind)

    def dec_div(x, y):
        if y == 0:
            raise EvalError(f"division by zero: DIV({V.format_decimal(x)}, {V.format_decimal(y)})")
        return convert(V.DEC.divide_int(x, y))

    return lift2(a, b, target, dec_div)


def _ieee_divide(x: float, y: float) -> float:
    if y == 0:
        if x == 0 or math.isnan(x):
            return NAN
        return math.copysign(INF, x) * math.copysign(1.0, y)
    try:
        return x / y
    except OverflowError:  # pragma: no cover - Python returns inf instead
        return math.copysign(INF, x) * math.copysign(1.0, y)


@register("IEEE_DIVIDE")
def ieee_divide(c, node, args, cx):
    arity("IEEE_DIVIDE", args, 2)
    a, b = _floats(c, "IEEE_DIVIDE", args)
    return lift2(a, b, T.FLOAT64, _ieee_divide)


@register_node(exp.SafeAdd)
def _map_safe_add(node):
    return "SAFE_ADD", args_of(node, "this", "expression")


@register_node(exp.SafeSubtract)
def _map_safe_subtract(node):
    return "SAFE_SUBTRACT", args_of(node, "this", "expression")


@register_node(exp.SafeMultiply)
def _map_safe_multiply(node):
    return "SAFE_MULTIPLY", args_of(node, "this", "expression")


@register_node(exp.SafeDivide)
def _map_safe_divide(node):
    return "SAFE_DIVIDE", args_of(node, "this", "expression")


@register_node(exp.SafeNegate)
def _map_safe_negate(node):
    return "SAFE_NEGATE", args_of(node, "this")


def _safe_binary(op: str, name: str):
    def handler(c, node, args, cx):
        from .expressions import numeric_binary

        arity(name, args, 2)
        a, b = (guard_argument(x) for x in args)
        if a.lit == "null" and b.lit == "null":
            raise AnalysisError(f"Operands of {name} cannot be literal NULL")
        if a.lit == "null":
            a = c.coerce(a, b.type) if b.type.is_numeric else a
        if b.lit == "null":
            b = c.coerce(b, a.type) if a.type.is_numeric else b
        if not (a.type.is_numeric and b.type.is_numeric):
            raise bad_signature(name, args)
        return safe_wrap(numeric_binary(c, op, a, b, op))

    return handler


register("SAFE_ADD")(_safe_binary("+", "SAFE_ADD"))
register("SAFE_SUBTRACT")(_safe_binary("-", "SAFE_SUBTRACT"))
register("SAFE_MULTIPLY")(_safe_binary("*", "SAFE_MULTIPLY"))
register("SAFE_DIVIDE")(_safe_binary("/", "SAFE_DIVIDE"))


@register("SAFE_NEGATE")
def safe_negate(c, node, args, cx):
    arity("SAFE_NEGATE", args, 1)
    a = args[0]
    if a.lit == "null":
        a = c.coerce(a, T.INT64)
    kind = a.type.kind
    if kind == "INT64":
        return lift1(a, T.INT64, lambda v: None if v == V.INT64_MIN else -v)
    if kind in ("NUMERIC", "BIGNUMERIC"):
        convert = V.decimal_of(kind)

        def negate(v):
            try:
                return convert(-v)
            except EvalError:
                return None

        return lift1(a, a.type, negate)
    if kind == "FLOAT64":
        return lift1(a, a.type, lambda v: -v)
    raise bad_signature("SAFE_NEGATE", args)


# --- square root, exponentials, logarithms, powers ---------------------------------------------------------------------


def _float_function(name: str, node_class, fn, exact_types=("NUMERIC", "BIGNUMERIC"), inexact: bool = True, spelling=None):
    """Register ``name(x)`` for FLOAT64: ``fn(x)`` on non-NULL, non-NaN-handled input (``fn`` may raise EvalError)."""

    if node_class is not None:

        @register_node(node_class)
        def _mapper(node, _name=name):
            return _name, args_of(node, "this")

    @register(*(spelling or (name,)))
    def handler(c, node, args, cx):
        arity(name, args, 1)
        (a,) = _floats(c, name, args, exact_types)
        af = a.fn

        def run(env):
            v = af(env)
            if v is None:
                return None
            result = fn(v)
            if inexact and _finite(v) and _finite(result) and result != 0:
                env.ctx.inexact = True
            return result

        return E(T.FLOAT64, run)

    return handler


def _sqrt(x: float) -> float:
    if x < 0:
        raise EvalError(f"Argument to SQRT cannot be negative: {V.format_double(x)}")
    return math.sqrt(x)


def _exp(x: float) -> float:
    try:
        return math.exp(x)
    except OverflowError:
        raise EvalError(f"double overflow: EXP({V.format_double(x)})") from None


def _ln(x: float) -> float:
    if x <= 0:
        raise EvalError(f"Argument to LN must be positive: {V.format_double(x)}")
    return math.log(x)


_float_function("SQRT", exp.Sqrt, _sqrt, inexact=False)
_float_function("EXP", exp.Exp, _exp)
_float_function("LN", exp.Ln, _ln)


@register_node(exp.Log)
def _map_log(node):
    # sqlglot: LOG(x, base) is Log(this=base, expression=x); LOG10(x) is Log(this=10, expression=x).
    found = args_of(node, "this", "expression")
    return "LOG", [node.args["expression"], node.args["this"]] if len(found) == 2 else found


def _log_two(x: float, base: float) -> float:
    """``LOG(x, base)`` with BigQuery's table of special cases (see the LOG documentation)."""

    if x == -INF or base == INF:
        return NAN
    if math.isnan(x) or math.isnan(base):
        if base == 1:
            raise Unsupported("LOG(NaN, 1)")
        return NAN
    if x <= 0 or base <= 0 or base == 1:
        raise EvalError(f"Invalid arguments to LOG: {V.format_double(x)}, {V.format_double(base)}")
    if x == INF:
        return INF if base > 1 else -INF
    if base == 10:
        return math.log10(x)
    return math.log(x) / math.log(base)


@register("LOG")
def log(c, node, args, cx):
    arity("LOG", args, 1, 2)
    floats = _floats(c, "LOG", args, ("NUMERIC", "BIGNUMERIC"))
    if len(floats) == 1:
        from .functions import REGISTRY

        return REGISTRY["LN"](c, node, floats, cx)
    a, b = floats
    af, bf = a.fn, b.fn

    def run(env):
        x, base = af(env), bf(env)
        if x is None or base is None:
            return None
        result = _log_two(x, base)
        if _finite(x) and _finite(base) and result != 0:
            env.ctx.inexact = True
        return result

    return E(T.FLOAT64, run)


@register_node(exp.Pow)
def _map_pow(node):
    return "POW", args_of(node, "this", "expression")


def _pow(x: float, y: float) -> float:
    if x == 0 and y == -INF:
        raise Unsupported("POW(0, -inf)")
    try:
        result = math.pow(x, y)
    except OverflowError:
        raise EvalError(f"double overflow: POW({V.format_double(x)}, {V.format_double(y)})") from None
    except ValueError:
        raise EvalError(f"Illegal arguments to POW: {V.format_double(x)}, {V.format_double(y)}") from None
    return result


@register("POW", "POWER")
def pow_(c, node, args, cx):
    arity("POW", args, 2)
    a, b = _floats(c, "POW", args, ("NUMERIC", "BIGNUMERIC"))
    af, bf = a.fn, b.fn

    def run(env):
        x, y = af(env), bf(env)
        if x is None or y is None:
            return None
        result = _pow(x, y)
        if _finite(x) and _finite(y) and x not in (0.0, 1.0) and y not in (0.0, 1.0) and _finite(result) and result != 0:
            env.ctx.inexact = True
        return result

    return E(T.FLOAT64, run)


# --- trigonometric and hyperbolic functions ----------------------------------------------------------------------------


def _periodic(fn):
    def apply(x: float) -> float:
        try:
            return fn(x)
        except ValueError:  # an infinity
            return NAN

    return apply


def _bounded(name: str, fn, lo: float, hi: float):
    def apply(x: float) -> float:
        if x < lo or x > hi:
            raise EvalError(f"Argument to {name} is out of domain: {V.format_double(x)}")
        return fn(x)

    return apply


def _hyperbolic(name: str, fn):
    def apply(x: float) -> float:
        try:
            return fn(x)
        except OverflowError:
            raise EvalError(f"double overflow: {name}({V.format_double(x)})") from None

    return apply


def _acosh(x: float) -> float:
    if x < 1:
        raise EvalError(f"Argument to ACOSH must be >= 1: {V.format_double(x)}")
    return math.acosh(x)


def _atanh(x: float) -> float:
    if x < -1 or x > 1:
        raise EvalError(f"Argument to ATANH is out of domain: {V.format_double(x)}")
    if x == 1:
        return INF
    if x == -1:
        return -INF
    return math.atanh(x)


def _reciprocal(name: str, fn, pole_error: bool = True):
    """``1 / fn(x)``: a zero denominator is an error, an infinity or NaN argument gives NaN as ``fn`` does."""

    def apply(x: float) -> float:
        d = fn(x)
        if d == 0:
            raise EvalError(f"division by zero: {name}({V.format_double(x)})")
        return 1.0 / d

    return apply


def _sech(x: float) -> float:
    try:
        d = math.cosh(x)
    except OverflowError:
        raise Unsupported("SECH of a large argument") from None
    return 1.0 / d


def _csch(x: float) -> float:
    if x == 0:
        raise EvalError("division by zero: CSCH(0)")
    try:
        d = math.sinh(x)
    except OverflowError:
        raise Unsupported("CSCH of a large argument") from None
    return 1.0 / d


def _coth(x: float) -> float:
    if x == 0:
        raise EvalError("division by zero: COTH(0)")
    return 1.0 / math.tanh(x)


def _cbrt(x: float) -> float:
    """The correctly rounded cube root (libm's ``cbrt`` is off by one bit on some platforms)."""

    if not _finite(x) or x == 0:
        return x
    guess = math.cbrt(x)
    exact = Fraction(x)
    best = guess
    for candidate in (guess, math.nextafter(guess, INF), math.nextafter(guess, -INF)):
        if abs(Fraction(candidate) ** 3 - exact) < abs(Fraction(best) ** 3 - exact):
            best = candidate
    return best


_float_function("SIN", exp.Sin, _periodic(math.sin))
_float_function("COS", exp.Cos, _periodic(math.cos))
_float_function("TAN", exp.Tan, _periodic(math.tan))
_float_function("ASIN", exp.Asin, _bounded("ASIN", math.asin, -1.0, 1.0))
_float_function("ACOS", exp.Acos, _bounded("ACOS", math.acos, -1.0, 1.0))
_float_function("ATAN", exp.Atan, math.atan)
_float_function("SINH", exp.Sinh, _hyperbolic("SINH", math.sinh))
_float_function("COSH", exp.Cosh, _hyperbolic("COSH", math.cosh))
_float_function("TANH", exp.Tanh, math.tanh)
_float_function("ASINH", exp.Asinh, math.asinh)
_float_function("ACOSH", exp.Acosh, _acosh)
_float_function("ATANH", exp.Atanh, _atanh)
_float_function("SEC", exp.Sec, lambda x: 1.0 / math.cos(x) if _finite(x) else NAN)
_float_function("CSC", exp.Csc, lambda x: _reciprocal("CSC", math.sin)(x) if _finite(x) else NAN)
_float_function("COT", exp.Cot, lambda x: _reciprocal("COT", math.tan)(x) if _finite(x) else NAN)
_float_function("SECH", exp.Sech, _sech)
_float_function("CSCH", exp.Csch, _csch)
_float_function("COTH", exp.Coth, _coth)
_float_function("CBRT", exp.Cbrt, _cbrt)


@register_node(exp.Atan2)
def _map_atan2(node):
    return "ATAN2", args_of(node, "this", "expression")


@register("ATAN2")
def atan2(c, node, args, cx):
    arity("ATAN2", args, 2)
    a, b = _floats(c, "ATAN2", args, ("NUMERIC", "BIGNUMERIC"))

    def fn(y, x):
        return math.atan2(y, x)

    return lift2(a, b, T.FLOAT64, fn)


@register_node(exp.Pi)
def _map_pi(node):
    return "PI", args_of(node)


@register("PI")
def pi(c, node, args, cx):
    arity("PI", args, 0)
    return E(T.FLOAT64, lambda env: math.pi)


# --- IS_INF, IS_NAN ----------------------------------------------------------------------------------------------------


@register_node(exp.IsInf)
def _map_is_inf(node):
    return "IS_INF", args_of(node, "this")


@register_node(exp.IsNan)
def _map_is_nan(node):
    return "IS_NAN", args_of(node, "this")


@register("IS_INF")
def is_inf(c, node, args, cx):
    arity("IS_INF", args, 1)
    (a,) = _floats(c, "IS_INF", args)
    return lift1(a, T.BOOL, math.isinf)


@register("IS_NAN")
def is_nan(c, node, args, cx):
    arity("IS_NAN", args, 1)
    (a,) = _floats(c, "IS_NAN", args)
    return lift1(a, T.BOOL, math.isnan)


# --- GREATEST, LEAST ---------------------------------------------------------------------------------------------------


@register_node(exp.Greatest)
def _map_greatest(node):
    if node.args.get("ignore_nulls"):
        raise Unsupported("GREATEST with IGNORE NULLS")
    return "GREATEST", args_of(node, "this", "expressions", flags=("ignore_nulls",))


@register_node(exp.Least)
def _map_least(node):
    if node.args.get("ignore_nulls"):
        raise Unsupported("LEAST with IGNORE NULLS")
    return "LEAST", args_of(node, "this", "expressions", flags=("ignore_nulls",))


def _extreme(name: str, pick_greater: bool):
    def handler(c, node, args, cx):
        arity(name, args, 1, 10**6)
        target, coerced = c.unify(args, name)
        if not T.comparable(target):
            raise AnalysisError(f"{name} is not defined for arguments of type {target}")
        from .expressions import less_fn

        less = less_fn(target)
        is_float = target.kind == "FLOAT64"
        fns = [e.fn for e in coerced]

        def run(env):
            values = [f(env) for f in fns]
            if any(v is None for v in values):
                return None
            if is_float and any(math.isnan(v) for v in values):
                return NAN
            best = values[0]
            for v in values[1:]:
                if (less(best, v) if pick_greater else less(v, best)):
                    best = v
            return best

        return E(target, run)

    return handler


register("GREATEST")(_extreme("GREATEST", True))
register("LEAST")(_extreme("LEAST", False))


# --- RANGE_BUCKET ------------------------------------------------------------------------------------------------------


@register_node(exp.RangeBucket)
def _map_range_bucket(node):
    return "RANGE_BUCKET", args_of(node, "this", "expression")


@register("RANGE_BUCKET")
def range_bucket(c, node, args, cx):
    arity("RANGE_BUCKET", args, 2)
    point, bounds = args
    if bounds.lit == "null":
        bounds = E(T.array(point.type if point.lit != "null" else T.INT64), bounds.fn, "null", None)
    if bounds.type.kind != "ARRAY":
        raise bad_signature("RANGE_BUCKET", args)
    target = T.supertype([(point.type, point.lit), (bounds.type.elem, None)])
    if not T.comparable(target):
        raise AnalysisError(f"RANGE_BUCKET is not defined for {target}")
    point = c.coerce(point, target)
    from .functions import coerce_array

    bounds = coerce_array(c, bounds, target, "RANGE_BUCKET")
    pf, bf = point.fn, bounds.fn
    is_float = target.kind == "FLOAT64"

    def run(env):
        x, array = pf(env), bf(env)
        if x is None or array is None:
            return None
        if is_float and math.isnan(x):
            raise Unsupported("RANGE_BUCKET of NaN")
        if any(b is None for b in array):
            raise Unsupported("RANGE_BUCKET with a NULL boundary")
        keys = [V.sort_key(target, b) for b in array]
        if any(keys[i] > keys[i + 1] for i in range(len(keys) - 1)):
            raise Unsupported("RANGE_BUCKET with unsorted boundaries")
        key = V.sort_key(target, x)
        return sum(1 for k in keys if not (key < k))

    return E(T.INT64, run)


# --- bits and hashes ---------------------------------------------------------------------------------------------------


@register_node(exp.BitwiseCount)
def _map_bit_count(node):
    return "BIT_COUNT", args_of(node, "this")


@register("BIT_COUNT")
def bit_count(c, node, args, cx):
    arity("BIT_COUNT", args, 1)
    a = args[0]
    if a.lit == "null":
        a = c.coerce(a, T.INT64)
    if a.type.kind == "INT64":
        return lift1(a, T.INT64, lambda v: bin(v & ((1 << 64) - 1)).count("1"))
    if a.type.kind == "BYTES":
        return lift1(a, T.INT64, lambda v: sum(bin(byte).count("1") for byte in v))
    raise bad_signature("BIT_COUNT", args)


def _digest(name: str, algorithm: str):
    def handler(c, node, args, cx):
        arity(name, args, 1)
        a = args[0]
        if a.lit == "null":
            a = c.coerce(a, T.BYTES)
        if a.type.kind == "STRING":
            return lift1(a, T.BYTES, lambda v: hashlib.new(algorithm, v.encode("utf-8")).digest())
        if a.type.kind == "BYTES":
            return lift1(a, T.BYTES, lambda v: hashlib.new(algorithm, v).digest())
        raise bad_signature(name, args)

    return handler


register("MD5")(_digest("MD5", "md5"))
register("SHA1")(_digest("SHA1", "sha1"))
register("SHA256")(_digest("SHA256", "sha256"))
register("SHA512")(_digest("SHA512", "sha512"))


@register_node(exp.MD5)
def _map_md5_hex(node):
    # sqlglot reads TO_HEX(MD5(x)) as one MD5 node (a lowercase hex string)
    return "<TO_HEX_MD5>", args_of(node, "this")


@register("<TO_HEX_MD5>")
def md5_hex(c, node, args, cx):
    arity("MD5", args, 1)
    digest = _digest("MD5", "md5")(c, node, args, cx)
    return lift1(digest, T.STRING, lambda v: v.hex())


@register_node(exp.MD5Digest)
def _map_md5(node):
    return "MD5", args_of(node, "this")


@register_node(exp.SHA1Digest)
def _map_sha1(node):
    return "SHA1", args_of(node, "this")


@register_node(exp.SHA2Digest)
def _map_sha2(node):
    # SHA256(x) and SHA512(x) both parse as SHA2Digest(this=x, length=256 or 512)
    arguments = args_of(node, "this", "length")
    length = node.args.get("length")
    if not isinstance(length, exp.Literal) or length.is_string or length.this not in ("256", "512"):
        raise Unsupported("SHA2 digest length")
    return f"SHA{length.this}", arguments[:1]
