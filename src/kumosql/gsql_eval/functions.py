"""Scalar function calls: sqlglot node -> BigQuery name and arguments -> typed closure.

Families live in ``fn_*.py`` modules that register here:

* ``@register_node(exp.Abs, ...)`` maps a sqlglot class to ``(NAME, [argument nodes])``. The mapper receives the node and
  must call ``_only`` for every argument it reads, so a flag sqlglot sets and the mapper ignores raises ``Unsupported``.
* ``@register("ABS", ...)`` evaluates a call by BigQuery name: ``handler(compiler, node, args: list[E], cx) -> E``.
  ``args`` are compiled and unchecked; the handler types them (``AnalysisError`` for a bad signature) and returns an E.
  Calls of an unknown name raise ``Unsupported``.
"""

from __future__ import annotations

from sqlglot import exp

from . import types as T
from .errors import AnalysisError, EvalError, Unsupported
from .runtime import E

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
            NODE_MAP[cls] = fn
        return fn

    return decorate


def safe_wrap(value: E) -> E:
    """``SAFE.f(...)``: a runtime error becomes NULL (analysis errors still raise)."""

    fn = value.fn

    def run(env):
        try:
            return fn(env)
        except EvalError:
            return None

    return E(value.type, run)


def compile_call(compiler, node, cx, safe: bool = False) -> E:
    mapper = NODE_MAP.get(type(node))
    if mapper is None:
        if isinstance(node, exp.Anonymous):
            name = str(node.this).upper()
            args = list(node.expressions)
        else:
            raise Unsupported(f"function {type(node).__name__}")
    else:
        name, args = mapper(node)
    return call_named(compiler, name, [compiler.expr(a, cx) for a in args], node, cx, safe=safe)


def call_named(compiler, name: str, args: list, node, cx, safe: bool = False) -> E:
    handler = REGISTRY.get(name.upper())
    if handler is None:
        raise Unsupported(f"function {name}")
    result = handler(compiler, node, args, cx)
    return safe_wrap(result) if safe else result


def array_concat(compiler, args: list) -> E:
    raise Unsupported("array concatenation")


# family modules register on import
from . import fn_array, fn_math, fn_misc, fn_string, fn_time  # noqa: E402,F401
