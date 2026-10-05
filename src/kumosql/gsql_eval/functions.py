"""Scalar function calls: sqlglot node -> BigQuery name and arguments -> typed closure.

Families live in ``fn_*.py`` modules that register here:

* ``@register_node(exp.Abs, ...)`` maps a sqlglot class to ``(NAME, [argument nodes])``. The mapper receives the node and
  must call ``_only`` for every argument it reads (``args_of`` does), so a flag sqlglot sets and the mapper ignores
  raises ``Unsupported``.
* ``@register("ABS", ...)`` evaluates a call by BigQuery name: ``handler(compiler, node, args: list[E], cx) -> E``.
  ``args`` are compiled and unchecked; the handler types them (``AnalysisError`` for a bad signature) and returns an E.
  Calls of an unknown name raise ``Unsupported``.
* A handler that accepts a lambda (``e -> e > 1``) is marked ``@takes_lambda``; it then receives a :class:`LambdaArg` in
  place of that argument's E (every other handler gets ``Unsupported`` for a lambda).

``SAFE.f(args)`` turns an error the function itself raises into NULL; an error raised while evaluating an argument still
propagates (the compliance tests check this), so every argument of a safe call is guarded by ``guard_argument``.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from sqlglot import exp

from . import types as T
from . import values as V
from .compiler import _only
from .errors import AnalysisError, EvalError, Unsupported
from .runtime import E, Env, const

NODE_MAP: dict = {}  # sqlglot class -> mapper(node) -> (NAME, [arg nodes])
REGISTRY: dict = {}  # NAME -> handler(compiler, node, args, cx) -> E


def register(*names):
    def decorate(fn):
        for name in names:
            REGISTRY[name.upper()] = fn
        return fn

    return decorate


def register_node(*classes):
    def decorate(fn):
        for cls in classes:
            if cls is not None:
                NODE_MAP[cls] = fn
        return fn

    return decorate


def takes_lambda(fn):
    """Mark a handler as accepting :class:`LambdaArg` arguments."""

    fn.takes_lambda = True
    return fn


@dataclass
class LambdaArg:
    """A lambda argument (``e -> body``): the sqlglot node and the compile context of the call."""

    node: exp.Lambda
    cx: Any


def args_of(node: exp.Expression, *keys: str, flags: tuple = ()) -> list:
    """The argument nodes of ``node`` stored under ``keys`` (in order, lists flattened, absent ones skipped).

    Refuses (``Unsupported``) a node carrying any other argument, so a flag sqlglot sets can never be ignored; ``flags``
    names arguments that are flags rather than expressions: they are allowed but not returned, and the mapper must
    check them itself.
    """

    _only(node, *keys, *flags)
    out: list = []
    for key in keys:
        value = node.args.get(key)
        if value is None:
            continue
        if isinstance(value, list):
            out.extend(value)
        else:
            out.append(value)
    return out


def named_args(node: exp.Anonymous) -> list:
    _only(node, "this", "expressions")
    return list(node.expressions)


# --- argument guards for SAFE. -----------------------------------------------------------------------------------------


class _ArgumentFailure(EvalError):
    """An argument of a SAFE call failed: the failure belongs to the argument, not to the function."""

    def __init__(self, error: EvalError):
        super().__init__(str(error))
        self.error = error


def guard_argument(value: Any) -> Any:
    if not isinstance(value, E):
        return value
    fn = value.fn

    def run(env):
        try:
            return fn(env)
        except EvalError as error:
            raise _ArgumentFailure(error) from None

    return E(value.type, run, value.lit, value.value)


def safe_wrap(value: E) -> E:
    """``SAFE.f(...)``: an error raised by ``f`` becomes NULL (analysis errors, and errors of the arguments, still raise)."""

    fn = value.fn

    def run(env):
        try:
            return fn(env)
        except _ArgumentFailure as failure:
            raise failure.error from None
        except EvalError:
            return None

    return E(value.type, run)


def compile_call(compiler, node, cx, safe: bool = False) -> E:
    mapper = NODE_MAP.get(type(node))
    if mapper is None:
        if isinstance(node, exp.Anonymous):
            name = str(node.this).upper()
            args = named_args(node)
        else:
            raise Unsupported(f"function {type(node).__name__}")
    else:
        name, args = mapper(node)
    compiled = [LambdaArg(a, cx) if isinstance(a, exp.Lambda) else compiler.expr(a, cx) for a in args]
    return call_named(compiler, name, compiled, node, cx, safe=safe)


def call_named(compiler, name: str, args: list, node, cx, safe: bool = False) -> E:
    handler = REGISTRY.get(name.upper())
    if handler is None:
        raise Unsupported(f"function {name}")
    if any(isinstance(a, LambdaArg) for a in args):
        if not getattr(handler, "takes_lambda", False):
            raise Unsupported(f"lambda argument of {name}")
        if safe:
            raise Unsupported("SAFE call with a lambda argument")
    if safe:
        args = [guard_argument(a) for a in args]
    result = handler(compiler, node, args, cx)
    return safe_wrap(result) if safe else result


# --- helpers for the function families ---------------------------------------------------------------------------------


def arity(name: str, args: list, low: int, high: int | None = None) -> None:
    """``AnalysisError`` unless ``low <= len(args) <= high``."""

    high = low if high is None else high
    if not low <= len(args) <= high:
        raise AnalysisError(f"No matching signature for function {name} for argument count {len(args)}")


def bad_signature(name: str, args: list) -> AnalysisError:
    return AnalysisError(f"No matching signature for function {name} for argument types: " + ", ".join(str(a.type) for a in args))


def coerce_to(compiler, name: str, value: E, target: T.Type) -> E:
    """``value`` as ``target`` (NULL literals, literals and widening); ``AnalysisError`` when it does not coerce."""

    if value.type == target or value.lit == "null":
        return compiler.coerce(value, target)
    if value.lit == "literal" or T.implicitly_coercible(value.type, target):
        try:
            return compiler.coerce(value, target)
        except AnalysisError:
            pass
    raise AnalysisError(f"No matching signature for function {name} for argument type: {value.type}")


def null_of(typ: T.Type) -> E:
    return E(typ, lambda env: None, "literal", None)


def lift1(arg: E, out: T.Type, fn) -> E:
    """``fn`` of one non-NULL argument; NULL in, NULL out."""

    af = arg.fn

    def run(env):
        v = af(env)
        return None if v is None else fn(v)

    return E(out, run)


def lift2(a: E, b: E, out: T.Type, fn) -> E:
    """``fn`` of two non-NULL arguments (both are evaluated, so an error in either surfaces); NULL in, NULL out."""

    af, bf = a.fn, b.fn

    def run(env):
        x, y = af(env), bf(env)
        return None if x is None or y is None else fn(x, y)

    return E(out, run)


def lift_n(args: list, out: T.Type, fn) -> E:
    """``fn(*values)`` of any number of non-NULL arguments (all evaluated); NULL in, NULL out."""

    fns = [a.fn for a in args]

    def run(env):
        values = [f(env) for f in fns]
        return None if any(v is None for v in values) else fn(*values)

    return E(out, run)


def mark_inexact(env: Env) -> None:
    env.ctx.inexact = True


def array_elements_in_order(env: Env, value: tuple, why: str) -> None:
    """Record that the result depends on the order of an array whose order BigQuery leaves unspecified."""

    if not V.ordered_kind(value):
        env.ctx.nondet(why)


def same_kind(source: tuple, items) -> tuple:
    """``items`` as a tuple of the same order-kind as ``source`` (an unordered array stays unordered)."""

    items = tuple(items)
    return V.UnorderedArray(items) if isinstance(source, V.UnorderedArray) else items


def coerce_array(compiler, value: E, elem: T.Type, name: str = "array") -> E:
    """An array (or NULL literal) as ``ARRAY<elem>`` by implicit element coercion."""

    target = T.array(elem)
    if value.type == target:
        return value
    if value.lit == "null":
        return E(target, value.fn, "null", None)
    if value.type.kind != "ARRAY" or not T.implicitly_coercible(value.type.elem, elem):
        raise AnalysisError(f"No matching signature for {name}: {value.type} does not coerce to {target}")
    convert = V.caster(value.type.elem, elem)
    fn = value.fn

    def run(env):
        v = fn(env)
        return None if v is None else same_kind(v, (None if x is None else convert(x, env.ctx.tz) for x in v))

    return E(target, run)


def is_empty_array_literal(value: E) -> bool:
    return value.lit == "literal" and value.type.kind == "ARRAY" and value.value == ()


def array_concat(compiler, args: list) -> E:
    """``ARRAY_CONCAT(a, b, ...)`` and ``a || b`` on arrays.

    Element types unify as in BigQuery (INT64 and FLOAT64 give ARRAY<FLOAT64>; an untyped ``[]`` or NULL takes the others'
    type). The result is NULL when any input is NULL, and unordered when any input is.
    """

    if not args:
        raise AnalysisError("ARRAY_CONCAT needs at least one argument")
    typed = []
    for a in args:
        if a.lit == "null" or is_empty_array_literal(a):
            continue
        if a.type.kind != "ARRAY":
            raise AnalysisError(f"No matching signature for ARRAY_CONCAT for argument type: {a.type}")
        typed.append(a)
    if not typed:
        elem = T.INT64
    else:
        try:
            elem = T.supertype([(a.type.elem, None) for a in typed])
        except AnalysisError:
            if any(a.type == T.array(T.INT64) for a in typed):
                raise Unsupported("array concatenation of different element types (an untyped empty array reads as ARRAY<INT64>)") from None
            raise
    if elem.kind == "ARRAY":
        raise AnalysisError("Arrays of arrays are not supported")
    parts = []
    for a in args:
        if is_empty_array_literal(a):
            parts.append(const(T.array(elem), ()))
        else:
            parts.append(coerce_array(compiler, a, elem, "ARRAY_CONCAT"))
    fns = [p.fn for p in parts]

    def run(env):
        values = [f(env) for f in fns]
        if any(v is None for v in values):
            return None
        out = tuple(x for v in values for x in v)
        return V.UnorderedArray(out) if any(isinstance(v, V.UnorderedArray) for v in values) else out

    return E(T.array(elem), run)


# family modules register on import
from . import fn_array, fn_math, fn_misc, fn_string, fn_time  # noqa: E402,F401
