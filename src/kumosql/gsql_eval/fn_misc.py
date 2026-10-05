"""Function family: errors (ERROR, NULLIFERROR, IFERROR, ISERROR), nondeterministic functions, TO_JSON_STRING.

Conditionals (IF, IFNULL, NULLIF, COALESCE) are handled in ``expressions.py``; FARM_FINGERPRINT, TYPEOF and the
account/session functions are not implemented, so a call of them raises ``Unsupported``.
"""

from __future__ import annotations

import random
import uuid

from sqlglot import exp

from . import types as T
from . import values as V
from .errors import EvalError, Unsupported
from .functions import arity, args_of, coerce_to, register, register_node
from .runtime import E

# --- ERROR and the functions that absorb errors --------------------------------------------------------------------------


@register("ERROR")
def error(c, node, args, cx):
    arity("ERROR", args, 1)
    message = coerce_to(c, "ERROR", args[0], T.STRING)
    mf = message.fn

    def run(env):
        text = mf(env)
        if text is None:
            raise Unsupported("ERROR(NULL)")
        raise EvalError(text)

    # ERROR returns a value of any type: it is typed like an untyped NULL, which every conditional and cast coerces.
    return E(T.INT64, run, "null", None)


@register("NULLIFERROR")
def nulliferror(c, node, args, cx):
    arity("NULLIFERROR", args, 1)
    (value,) = args
    fn = value.fn

    def run(env):
        try:
            return fn(env)
        except EvalError:
            return None

    return E(value.type, run)


@register("ISERROR")
def iserror(c, node, args, cx):
    arity("ISERROR", args, 1)
    fn = args[0].fn

    def run(env):
        try:
            fn(env)
        except EvalError:
            return True
        return False

    return E(T.BOOL, run)


@register("IFERROR")
def iferror(c, node, args, cx):
    arity("IFERROR", args, 2)
    result_type, (attempt, fallback) = c.unify(list(args), "IFERROR")
    af, ff = attempt.fn, fallback.fn

    def run(env):
        try:
            return af(env)
        except EvalError:
            return ff(env)

    return E(result_type, run)


# --- nondeterministic functions ----------------------------------------------------------------------------------------


@register_node(exp.Uuid)
def _map_uuid(node):
    if not node.args.get("is_string"):
        raise Unsupported("UUID that is not a string")
    return "GENERATE_UUID", args_of(node, flags=("is_string",))


@register("GENERATE_UUID")
def generate_uuid(c, node, args, cx):
    arity("GENERATE_UUID", args, 0)

    def run(env):
        env.ctx.nondet("GENERATE_UUID")
        return str(uuid.uuid4())

    return E(T.STRING, run)


@register_node(exp.Rand)
def _map_rand(node):
    return "RAND", args_of(node)


@register("RAND")
def rand(c, node, args, cx):
    arity("RAND", args, 0)

    def run(env):
        env.ctx.nondet("RAND")
        return random.random()

    return E(T.FLOAT64, run)


# --- JSON ----------------------------------------------------------------------------------------------------


@register_node(exp.JSONFormat)
def _map_to_json_string(node):
    return "TO_JSON_STRING", args_of(node, "this", "options")


_JSON_ESCAPES = {'"': '\\"', "\\": "\\\\", "\b": "\\b", "\f": "\\f", "\n": "\\n", "\r": "\\r", "\t": "\\t"}


def _json_string(text: str) -> str:
    out = ['"']
    for char in text:
        if char in _JSON_ESCAPES:
            out.append(_JSON_ESCAPES[char])
        elif ord(char) < 0x20:
            out.append(f"\\u{ord(char):04x}")
        elif ord(char) == 0x7F or 0xD800 <= ord(char) <= 0xDFFF:
            raise Unsupported("TO_JSON_STRING of a string with a DEL or surrogate character")
        else:
            out.append(char)
    out.append('"')
    return "".join(out)


def _json_writer(typ: T.Type):
    """A function ``(payload, env)`` giving compact JSON text (BOOL, INT64, STRING, and arrays/structs of them)."""

    kind = typ.kind
    if kind == "BOOL":
        return lambda v, env: "null" if v is None else ("true" if v else "false")
    if kind == "INT64":
        return lambda v, env: "null" if v is None else str(v)
    if kind == "STRING":
        return lambda v, env: "null" if v is None else _json_string(v)
    if kind == "ARRAY":
        inner = _json_writer(typ.elem)

        def write_array(v, env):
            if v is None:
                return "null"
            if not V.ordered_kind(v):
                env.ctx.nondet("TO_JSON_STRING of an unordered array")
            return "[" + ",".join(inner(x, env) for x in v) + "]"

        return write_array
    if kind == "STRUCT":
        if any(name is None or name == "" for name, _ in typ.fields):
            raise Unsupported("TO_JSON_STRING of a struct with unnamed fields")
        if any("\\" in name for name, _ in typ.fields):
            raise Unsupported("TO_JSON_STRING of a struct whose field names hold escapes (sqlglot keeps identifier escapes raw)")
        names = [_json_string(name) for name, _ in typ.fields]
        writers = [_json_writer(t) for _, t in typ.fields]

        def write_struct(v, env):
            if v is None:
                return "null"
            return "{" + ",".join(f"{n}:{w(x, env)}" for n, w, x in zip(names, writers, v)) + "}"

        return write_struct
    raise Unsupported(f"TO_JSON_STRING of {typ}")


@register("TO_JSON_STRING")
def to_json_string(c, node, args, cx):
    arity("TO_JSON_STRING", args, 1, 2)
    value = args[0]
    if value.lit == "null":
        return E(T.STRING, lambda env: "null")
    if len(args) == 2:
        pretty = args[1]
        if not (pretty.lit == "literal" and pretty.type == T.BOOL and pretty.value is False):
            raise Unsupported("TO_JSON_STRING with a pretty-print argument")
    write = _json_writer(value.type)
    fn = value.fn
    return E(T.STRING, lambda env: write(fn(env), env))
