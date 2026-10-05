"""Function family: arrays (length, slices, concatenation, GENERATE_ARRAY, ARRAY_INCLUDES, lambdas).

Arrays whose order BigQuery leaves unspecified (:class:`values.UnorderedArray`) stay unordered through the functions that
keep order (ARRAY_REVERSE, ARRAY_SLICE, ARRAY_CONCAT) and make the result nondeterministic when a function picks an
element by position (ARRAY_FIRST, ARRAY_LAST, ARRAY_TO_STRING).
"""

from __future__ import annotations

import math
from datetime import date
from decimal import Decimal

from sqlglot import exp

from . import types as T
from . import values as V
from .compiler import Cx, FromScope, Source
from .errors import AnalysisError, EvalError, Unsupported
from .functions import (
    LambdaArg,
    arity,
    args_of,
    array_concat,
    array_elements_in_order,
    bad_signature,
    coerce_array,
    coerce_to,
    is_empty_array_literal,
    register,
    register_node,
    same_kind,
    takes_lambda,
)
from .runtime import E, const

MAX_GENERATED = 16000  # beyond this many elements the exact BigQuery limit is not known to this evaluator


def _array_arg(c, name: str, value: E, elem_hint: T.Type = T.INT64) -> E:
    """An array argument; a NULL literal becomes a NULL array, anything else must be an array."""

    if value.lit == "null":
        return E(T.array(elem_hint), value.fn, "null", None)
    if value.type.kind != "ARRAY":
        raise bad_signature(name, [value])
    return value


# --- ARRAY_LENGTH, ARRAY_REVERSE ---------------------------------------------------------------------------------------


@register_node(exp.ArraySize)
def _map_size(node):
    return "ARRAY_LENGTH", args_of(node, "this")


@register("ARRAY_LENGTH")
def array_length(c, node, args, cx):
    arity("ARRAY_LENGTH", args, 1)
    a = _array_arg(c, "ARRAY_LENGTH", args[0])
    af = a.fn

    def run(env):
        v = af(env)
        return None if v is None else len(v)

    return E(T.INT64, run)


@register_node(exp.ArrayReverse)
def _map_reverse(node):
    return "ARRAY_REVERSE", args_of(node, "this")


@register("ARRAY_REVERSE")
def array_reverse(c, node, args, cx):
    arity("ARRAY_REVERSE", args, 1)
    a = _array_arg(c, "ARRAY_REVERSE", args[0])
    af = a.fn

    def run(env):
        v = af(env)
        return None if v is None else same_kind(v, reversed(v))

    return E(a.type, run)


# --- ARRAY_FIRST, ARRAY_LAST, ARRAY_SLICE ------------------------------------------------------------------------------


@register_node(exp.ArrayFirst)
def _map_first(node):
    return "ARRAY_FIRST", args_of(node, "this")


@register_node(exp.ArrayLast)
def _map_last(node):
    return "ARRAY_LAST", args_of(node, "this")


def _end_element(name: str, index: int, what: str):
    def handler(c, node, args, cx):
        arity(name, args, 1)
        a = _array_arg(c, name, args[0])
        af = a.fn

        def run(env):
            v = af(env)
            if v is None:
                return None
            if not v:
                raise EvalError(f"{name} cannot get the {what} element of an empty array")
            array_elements_in_order(env, v, f"{name} of an unordered array")
            return v[index]

        return E(a.type.elem, run)

    return handler


register("ARRAY_FIRST")(_end_element("ARRAY_FIRST", 0, "first"))
register("ARRAY_LAST")(_end_element("ARRAY_LAST", -1, "last"))


@register_node(exp.ArraySlice)
def _map_slice(node):
    return "ARRAY_SLICE", args_of(node, "this", "start", "end")


@register("ARRAY_SLICE")
def array_slice(c, node, args, cx):
    arity("ARRAY_SLICE", args, 3)
    a = _array_arg(c, "ARRAY_SLICE", args[0])
    start = coerce_to(c, "ARRAY_SLICE", args[1], T.INT64)
    end = coerce_to(c, "ARRAY_SLICE", args[2], T.INT64)
    af, sf, ef = a.fn, start.fn, end.fn

    def run(env):
        v, s, e = af(env), sf(env), ef(env)
        if v is None or s is None or e is None:
            return None
        n = len(v)
        s = s + n if s < 0 else s
        e = e + n if e < 0 else e
        s, e = max(s, 0), min(e, n - 1)
        return same_kind(v, v[s : e + 1] if s <= e else ())

    return E(a.type, run)


# --- ARRAY_TO_STRING ---------------------------------------------------------------------------------------------------


@register_node(exp.ArrayToString)
def _map_to_string(node):
    return "ARRAY_TO_STRING", args_of(node, "this", "expression", "null")


@register("ARRAY_TO_STRING")
def array_to_string(c, node, args, cx):
    arity("ARRAY_TO_STRING", args, 2, 3)
    a = _array_arg(c, "ARRAY_TO_STRING", args[0], T.STRING)
    if a.type.elem.kind not in ("STRING", "BYTES"):
        raise bad_signature("ARRAY_TO_STRING", args)
    kind = a.type.elem
    delimiter = coerce_to(c, "ARRAY_TO_STRING", args[1], kind)
    null_text = coerce_to(c, "ARRAY_TO_STRING", args[2], kind) if len(args) > 2 else None
    af, df = a.fn, delimiter.fn
    nf = null_text.fn if null_text is not None else None

    def run(env):
        v, d = af(env), df(env)
        t = nf(env) if nf is not None else None
        if v is None or d is None:
            return None
        if nf is not None and t is None:
            raise Unsupported("ARRAY_TO_STRING with a NULL null_text")
        array_elements_in_order(env, v, "ARRAY_TO_STRING of an unordered array")
        parts = [(t if x is None else x) for x in v if x is not None or t is not None]
        return d.join(parts)

    return E(kind, run)


# --- ARRAY_CONCAT ------------------------------------------------------------------------------------------------------


@register_node(exp.ArrayConcat)
def _map_concat(node):
    if node.args.get("null_propagation"):
        raise Unsupported("ARRAY_CONCAT that propagates NULL arrays")
    return "ARRAY_CONCAT", args_of(node, "this", "expressions", flags=("null_propagation",))


@register("ARRAY_CONCAT")
def array_concat_call(c, node, args, cx):
    return array_concat(c, args)


# --- ARRAY_IS_DISTINCT, ARRAY_INCLUDES, _ANY, _ALL ----------------------------------------------------------------------


@register("ARRAY_IS_DISTINCT")
def array_is_distinct(c, node, args, cx):
    arity("ARRAY_IS_DISTINCT", args, 1)
    a = _array_arg(c, "ARRAY_IS_DISTINCT", args[0])
    elem = a.type.elem
    if not T.groupable(elem):
        raise AnalysisError(f"ARRAY_IS_DISTINCT is not defined for arrays of {elem}")
    af = a.fn

    def run(env):
        v = af(env)
        if v is None:
            return None
        keys = [V.group_key(elem, x) for x in v]
        return len(set(keys)) == len(keys)

    return E(T.BOOL, run)


def _unify_with_elements(c, name: str, array: E, other: E, other_is_array: bool) -> tuple[T.Type, E, E]:
    """The element type shared by ``array`` and ``other`` (an array or a single value) and both coerced to it."""

    other_type = other.type.elem if other_is_array else other.type
    other_lit = None if other_is_array else other.lit
    if other.lit == "null":
        target = array.type.elem
    else:
        try:
            target = T.supertype([(array.type.elem, None), (other_type, other_lit)])
        except AnalysisError:
            raise bad_signature(name, [array, other]) from None
    if not T.equatable(target):
        raise AnalysisError(f"{name} is not defined for elements of type {target}")
    left = coerce_array(c, array, target, name)
    right = coerce_array(c, other, target, name) if other_is_array else c.coerce(other, target)
    return target, left, right


@register("ARRAY_INCLUDES")
@takes_lambda
def array_includes(c, node, args, cx):
    arity("ARRAY_INCLUDES", args, 2)
    array, probe = args
    if isinstance(array, LambdaArg):
        raise bad_signature("ARRAY_INCLUDES", [])
    array = _array_arg(c, "ARRAY_INCLUDES", array)
    if isinstance(probe, LambdaArg):
        return _includes_lambda(c, array, probe)
    target, left, right = _unify_with_elements(c, "ARRAY_INCLUDES", array, probe, False)
    lf, rf = left.fn, right.fn

    def run(env):
        values, x = lf(env), rf(env)
        if values is None or x is None:
            return None
        return any(V.sql_equal(target, x, y) is True for y in values)

    return E(T.BOOL, run)


def _includes_lambda(c, array: E, lam: LambdaArg) -> E:
    if len(lam.node.expressions) != 1:
        raise AnalysisError("The lambda of ARRAY_INCLUDES takes one argument")
    if lam.cx.group is not None:
        raise Unsupported("lambda after GROUP BY")
    param = lam.node.expressions[0]
    if not isinstance(param, exp.Identifier):
        raise Unsupported("lambda parameter")
    scope = FromScope([Source(None, [(param.name, 0, array.type.elem)])], 1, lam.cx.scope)
    name = param.name.lower()

    def as_column(n):
        # a lambda parameter used in the body is a bare Identifier in the tree, not a Column
        if isinstance(n, exp.Identifier) and n.name.lower() == name and not isinstance(n.parent, (exp.Column, exp.Dot)):
            return exp.column(n.copy())
        return n

    body = c.expr(lam.node.this.copy().transform(as_column), Cx(scope, lam.cx.replace))
    if body.lit == "null":
        body = c.coerce(body, T.BOOL)
    if body.type != T.BOOL:
        raise AnalysisError("The lambda of ARRAY_INCLUDES must return BOOL")
    af, bf = array.fn, body.fn

    def run(env):
        values = af(env)
        if values is None:
            return None
        for x in values:
            if bf(env.child((x,))) is True:
                return True
        return False

    return E(T.BOOL, run)


def _includes_many(name: str, require_all: bool):
    def handler(c, node, args, cx):
        arity(name, args, 2)
        array = _array_arg(c, name, args[0])
        probe = _array_arg(c, name, args[1], array.type.elem)
        if is_empty_array_literal(probe):
            probe = E(T.array(array.type.elem), probe.fn, probe.lit, probe.value)
        target, left, right = _unify_with_elements(c, name, array, probe, True)
        lf, rf = left.fn, right.fn

        def run(env):
            values, wanted = lf(env), rf(env)
            if values is None or wanted is None:
                return None
            present = [y for y in values if y is not None]

            def found(x):
                return x is not None and any(V.sql_equal(target, x, y) is True for y in present)

            return all(found(x) for x in wanted) if require_all else any(found(x) for x in wanted)

        return E(T.BOOL, run)

    return handler


register("ARRAY_INCLUDES_ANY")(_includes_many("ARRAY_INCLUDES_ANY", False))
register("ARRAY_INCLUDES_ALL")(_includes_many("ARRAY_INCLUDES_ALL", True))


# --- GENERATE_ARRAY, GENERATE_DATE_ARRAY -------------------------------------------------------------------------------


@register_node(exp.GenerateSeries)
def _map_generate_array(node):
    return "GENERATE_ARRAY", args_of(node, "start", "end", "step")


def _generate_numbers(kind: str, start, end, step):
    if step == 0:
        raise EvalError("Sequence step cannot be 0.")
    if kind == "INT64":
        stop = end + 1 if step > 0 else end - 1
        if (stop - start) // step > MAX_GENERATED:
            raise Unsupported("GENERATE_ARRAY beyond 16000 elements")
        return tuple(range(start, stop, step))
    if kind in ("NUMERIC", "BIGNUMERIC"):
        count = 0 if (step > 0 and start > end) or (step < 0 and start < end) else int((end - start) // step) + 1
        if count > MAX_GENERATED:
            raise Unsupported("GENERATE_ARRAY beyond 16000 elements")
        convert = V.decimal_of(kind)
        return tuple(convert(start + step * i) for i in range(count))
    # FLOAT64: exact only when every element is an integer that doubles represent
    if any(math.isnan(x) or math.isinf(x) for x in (start, end, step)):
        raise Unsupported("GENERATE_ARRAY with a NaN or infinite FLOAT64")
    if start != int(start) or step != int(step) or abs(start) > 2**52 or abs(end) > 2**52 or abs(step) > 2**52:
        raise Unsupported("GENERATE_ARRAY of non-integral doubles (the accumulation order is not known)")
    return tuple(float(x) for x in _generate_numbers("INT64", int(start), math.floor(end) if step > 0 else math.ceil(end), int(step)))


@register("GENERATE_ARRAY")
def generate_array(c, node, args, cx):
    arity("GENERATE_ARRAY", args, 2, 3)
    for a in args:
        if a.lit != "null" and not a.type.is_numeric:
            raise bad_signature("GENERATE_ARRAY", args)
    target = T.supertype([(a.type, a.lit) for a in args])
    coerced = [coerce_to(c, "GENERATE_ARRAY", a, target) for a in args]
    fns = [e.fn for e in coerced]
    one = Decimal(1) if target.kind in ("NUMERIC", "BIGNUMERIC") else (1.0 if target.kind == "FLOAT64" else 1)

    def run(env):
        values = [f(env) for f in fns]
        if any(v is None for v in values):
            return None
        start, end = values[0], values[1]
        step = values[2] if len(values) > 2 else one
        return _generate_numbers(target.kind, start, end, step)

    return E(T.array(target), run)


@register_node(exp.GenerateDateArray)
def _map_generate_date_array(node):
    return "GENERATE_DATE_ARRAY", args_of(node, "start", "end", "step")


@register("GENERATE_DATE_ARRAY")
def generate_date_array(c, node, args, cx):
    arity("GENERATE_DATE_ARRAY", args, 2, 3)
    start = coerce_to(c, "GENERATE_DATE_ARRAY", args[0], T.DATE)
    end = coerce_to(c, "GENERATE_DATE_ARRAY", args[1], T.DATE)
    step = args[2] if len(args) > 2 else None
    if step is not None:
        if step.lit == "null":
            step = c.coerce(step, T.INTERVAL)
        if step.type != T.INTERVAL:
            raise bad_signature("GENERATE_DATE_ARRAY", args)
    sf, ef = start.fn, end.fn
    stf = step.fn if step is not None else None

    def run(env):
        first, last = sf(env), ef(env)
        interval = stf(env) if stf is not None else V.Interval(0, 1, 0)
        if first is None or last is None or interval is None:
            return None
        if interval.micros != 0 or (interval.months != 0 and interval.days != 0):
            raise Unsupported("GENERATE_DATE_ARRAY step that is not a whole number of one date part")
        if interval.months == 0 and interval.days == 0:
            raise EvalError("Sequence step cannot be 0.")
        out = []
        if interval.months:
            if first.day > 28:
                raise Unsupported("GENERATE_DATE_ARRAY by months from a day beyond the 28th")
            i = 0
            while True:
                current = V.add_months(first, interval.months * i)
                if (interval.months > 0 and current > last) or (interval.months < 0 and current < last):
                    break
                out.append(current)
                i += 1
                if len(out) > MAX_GENERATED:
                    raise Unsupported("GENERATE_DATE_ARRAY beyond 16000 elements")
            return tuple(out)
        step_days = interval.days
        count = 0
        if (step_days > 0 and first <= last) or (step_days < 0 and first >= last):
            count = abs((last - first).days) // abs(step_days) + 1
        if count > MAX_GENERATED:
            raise Unsupported("GENERATE_DATE_ARRAY beyond 16000 elements")
        return tuple(V.date_add_days(first, step_days * i) for i in range(count))

    return E(T.array(T.DATE), run)
