"""STRING and BYTES functions.

Every function here either returns what BigQuery returns or raises :class:`Unsupported`; a data-dependent doubt
(a non-ASCII letter whose case mapping may differ between Unicode versions, an empty search string whose BigQuery
result is not documented, a regular expression construct RE2 reads differently from Python's ``re``) raises
``Unsupported`` when the value is met, never a guess.

Layout: shared typing helpers, then the plain functions (length, case, trim, pad, replace, substring, search, split,
code points, translate, encodings), then regular expressions (an RE2 to ``re`` translator that accepts only the
subset both read the same way), then ``FORMAT``, ``SOUNDEX``, ``EDIT_DISTANCE`` and ``NORMALIZE``.

sqlglot traps handled by the mappers (verified on sqlglot 30.21): ``LENGTH`` carries ``binary`` while ``CHAR_LENGTH``
does not; ``LTRIM``/``RTRIM``/``LPAD``/``RPAD`` are one node with a flag; ``INITCAP`` gets default delimiters written
in; ``SPLIT`` gets a default ``","``; ``REGEXP_EXTRACT`` and ``_ALL`` get a ``group`` derived by compiling the pattern
with Python's ``re``; ``NORMALIZE`` keeps its mode as a name, not an expression; ``CONTAINS_SUBSTR`` is read as
``LOWER(a) CONTAINS LOWER(b)`` (an approximation, so left unregistered).
"""

from __future__ import annotations

import base64
import binascii
import codecs
import re
import unicodedata
from functools import lru_cache

from sqlglot import exp

from . import types as T
from . import values as V
from .compiler import _only
from .errors import AnalysisError, EvalError, Unsupported
from .functions import register, register_node
from .runtime import E

# --------------------------------------------------------------------------------------------------------------
# shared helpers
# --------------------------------------------------------------------------------------------------------------

_TEXT = ("STRING", "BYTES")
_MAX_OUT_SURE = 1 << 20  # beyond this many characters every reading of "1MB" is exceeded
_MAX_OUT_MAYBE = 1_000_000  # below this many bytes no reading of "1MB" is exceeded


def _arg(node: exp.Expression, key: str):
    """An argument node, ``None`` when absent (sqlglot stores some absent arguments as ``False``)."""

    value = node.args.get(key)
    return None if value is None or value is False else value


def _node_args(node: exp.Expression, *keys: str) -> list:
    """The argument nodes for ``keys`` in order, skipping the absent ones."""

    return [value for value in (_arg(node, key) for key in keys) if value is not None]


def _text_args(c, args: list, what: str) -> tuple:
    """The common STRING/BYTES type of ``args`` (an untyped NULL takes the other's) and the coerced arguments."""

    if all(a.lit == "null" for a in args):
        target = T.STRING
    else:
        target = T.supertype([(a.type, a.lit) for a in args])
    if target.kind not in _TEXT:
        raise AnalysisError(f"No matching signature for function {what} for argument types: " + ", ".join(str(a.type) for a in args))
    return target, [c.coerce(a, target, what) for a in args]


def _string_arg(c, arg: E, what: str) -> E:
    if arg.lit == "null":
        return c.coerce(arg, T.STRING)
    if arg.type != T.STRING:
        raise AnalysisError(f"No matching signature for function {what} for argument type: {arg.type}")
    return arg


def _int_arg(c, arg: E, what: str) -> E:
    if arg.lit == "null":
        return c.coerce(arg, T.INT64)
    if arg.type != T.INT64:
        raise AnalysisError(f"No matching signature for function {what}: INT64 expected, got {arg.type}")
    return arg


def _arity(args: list, low: int, high: int, what: str) -> None:
    if not low <= len(args) <= high:
        raise AnalysisError(f"No matching signature for function {what} with {len(args)} arguments")


def _strict(fn, *values: E):
    """A closure calling ``fn`` on the argument values, NULL when any is NULL (all are evaluated first)."""

    fns = [v.fn for v in values]
    if len(fns) == 1:
        f0 = fns[0]

        def run1(env):
            x = f0(env)
            return None if x is None else fn(x)

        return run1
    if len(fns) == 2:
        f0, f1 = fns

        def run2(env):
            x, y = f0(env), f1(env)
            return None if x is None or y is None else fn(x, y)

        return run2

    def run(env):
        vals = [f(env) for f in fns]
        return None if any(v is None for v in vals) else fn(*vals)

    return run


def _utf8_len(value: str) -> int:
    try:
        return len(value.encode("utf-8"))
    except UnicodeEncodeError:
        raise Unsupported("string with a lone surrogate") from None


def _check_output(chars: int, text_kind: str, result: str | bytes | None = None) -> None:
    """BigQuery fails an output over 1MB; between the two readings of "1MB" the answer is unknown."""

    if chars > _MAX_OUT_SURE:
        raise EvalError("Output exceeds max allowed output size of 1MB")
    if result is not None:
        size = len(result) if text_kind == "BYTES" else _utf8_len(result)
        if size > _MAX_OUT_MAYBE:
            raise Unsupported("output near the 1MB limit")


# --------------------------------------------------------------------------------------------------------------
# CONCAT, lengths
# --------------------------------------------------------------------------------------------------------------


@register_node(exp.Concat)
def _concat_node(node):
    _only(node, "expressions", "safe")
    return "CONCAT", list(node.expressions)


@register("CONCAT")
def _concat(c, node, args, cx):
    if not args:
        raise AnalysisError("No matching signature for function CONCAT with no arguments")
    if any(a.lit != "null" and a.type.kind not in _TEXT for a in args):
        # BigQuery's CONCAT takes only STRING or BYTES. The GoogleSQL reference, under a language feature BigQuery lacks
        # (CONCAT_MIXED_TYPES), casts every argument to STRING: that is what mode="googlesql" evaluates.
        if c.mode != "googlesql" or any(a.lit != "null" and a.type.kind == "BYTES" for a in args):
            raise AnalysisError("No matching signature for function CONCAT for argument types: " + ", ".join(str(a.type) for a in args))
        types = [T.STRING if a.lit == "null" else a.type for a in args]
        fns = [a.fn for a in args]

        def run(env):
            vals = [f(env) for f in fns]
            if any(v is None for v in vals):
                return None
            return "".join(V.to_string(t, v, env.ctx.tz) for t, v in zip(types, vals))

        return E(T.STRING, run)
    target, args = _text_args(c, args, "CONCAT")
    return E(target, _strict(lambda *vals: vals[0][:0].join(vals), *args))


@register_node(exp.Length)
def _length_node(node):
    _only(node, "this", "binary")
    return ("LENGTH" if node.args.get("binary") else "CHAR_LENGTH"), [node.this]


@register("LENGTH")
def _length(c, node, args, cx):
    _arity(args, 1, 1, "LENGTH")
    target, (a,) = _text_args(c, args, "LENGTH")
    return E(T.INT64, _strict(len, a))


@register("CHAR_LENGTH", "CHARACTER_LENGTH")
def _char_length(c, node, args, cx):
    _arity(args, 1, 1, "CHAR_LENGTH")
    a = _string_arg(c, args[0], "CHAR_LENGTH")
    return E(T.INT64, _strict(len, a))


@register_node(exp.ByteLength)
def _byte_length_node(node):
    _only(node, "this")
    return "BYTE_LENGTH", [node.this]


@register("BYTE_LENGTH", "OCTET_LENGTH")
def _byte_length(c, node, args, cx):
    _arity(args, 1, 1, "BYTE_LENGTH")
    target, (a,) = _text_args(c, args, "BYTE_LENGTH")
    return E(T.INT64, _strict(len if target.kind == "BYTES" else _utf8_len, a))


# --------------------------------------------------------------------------------------------------------------
# LOWER, UPPER, INITCAP
# --------------------------------------------------------------------------------------------------------------


@lru_cache(maxsize=None)
def _caseless(ch: str) -> bool:
    """A non-ASCII character that no Unicode version maps to another case (so Python and BigQuery agree on it)."""

    if unicodedata.category(ch) in ("Lu", "Ll", "Lt", "Cn", "Cs"):
        return False
    return ch.lower() == ch and ch.upper() == ch and ch.title() == ch and ch.casefold() == ch


def _simple_case_safe(value: str, lower: bool) -> None:
    """Decline a character whose mapping is not one code point to one code point: engines differ on multi-character and
    context-dependent mappings (``ß``, ``İ``, a final sigma), and on characters this Unicode version does not assign."""

    if value.isascii():
        return
    for ch in value:
        if ch.isascii():
            continue
        if unicodedata.category(ch) == "Cn":
            raise Unsupported("case mapping of a character unassigned in this Unicode version")
        if len(ch.lower()) != 1 or len(ch.upper()) != 1 or (lower and ch == "\u03a3"):
            raise Unsupported("case mapping that is not one code point to one code point")


def _case_safe(value: str) -> None:
    """For INITCAP (title case may differ from upper case): only characters with no case at all."""

    if value.isascii():
        return
    for ch in value:
        if not ch.isascii() and not _caseless(ch):
            raise Unsupported("case mapping of a non-ASCII letter (Unicode version dependent)")


def _lower(value):
    if isinstance(value, bytes):
        return value.lower()
    _simple_case_safe(value, True)
    return value.lower()


def _upper(value):
    if isinstance(value, bytes):
        return value.upper()
    _simple_case_safe(value, False)
    return value.upper()


@register_node(exp.Lower)
def _lower_node(node):
    _only(node, "this")
    return "LOWER", [node.this]


@register_node(exp.Upper)
def _upper_node(node):
    _only(node, "this")
    return "UPPER", [node.this]


@register("LOWER")
def _lower_fn(c, node, args, cx):
    _arity(args, 1, 1, "LOWER")
    target, (a,) = _text_args(c, args, "LOWER")
    return E(target, _strict(_lower, a))


@register("UPPER")
def _upper_fn(c, node, args, cx):
    _arity(args, 1, 1, "UPPER")
    target, (a,) = _text_args(c, args, "UPPER")
    return E(target, _strict(_upper, a))


def _sqlglot_default_delimiters() -> str:
    import sqlglot

    node = sqlglot.parse_one("SELECT INITCAP(a)", read="bigquery").expressions[0]
    return node.expression.name


_INITCAP_DEFAULT = _sqlglot_default_delimiters()


@register_node(exp.Initcap)
def _initcap_node(node):
    _only(node, "this", "expression")
    delimiters = _arg(node, "expression")
    if delimiters is None:
        raise Unsupported("INITCAP without delimiters")
    if isinstance(delimiters, exp.Literal) and delimiters.is_string and delimiters.name == _INITCAP_DEFAULT:
        # sqlglot writes this string in for ``INITCAP(x)``; it cannot be told from a call that spells it out, and the
        # default delimiter set of BigQuery is not something this evaluator can verify
        raise Unsupported("INITCAP with the default delimiters")
    return "INITCAP", [node.this, delimiters]


def _initcap(value: str, delimiters: str) -> str:
    _case_safe(value)
    out = []
    word_start = True
    for ch in value:
        if ch in delimiters:
            out.append(ch)
            word_start = True
        elif word_start:
            out.append(ch.upper())
            word_start = False
        else:
            out.append(ch.lower())
    return "".join(out)


@register("INITCAP")
def _initcap_fn(c, node, args, cx):
    _arity(args, 2, 2, "INITCAP")
    a, d = _string_arg(c, args[0], "INITCAP"), _string_arg(c, args[1], "INITCAP")
    return E(T.STRING, _strict(_initcap, a, d))


# --------------------------------------------------------------------------------------------------------------
# TRIM, LTRIM, RTRIM
# --------------------------------------------------------------------------------------------------------------


@register_node(exp.Trim)
def _trim_node(node):
    _only(node, "this", "expression", "position")
    position = _arg(node, "position")
    if position is None:
        name = "TRIM"
    elif isinstance(position, str) and position.upper() in ("LEADING", "TRAILING"):
        name = "LTRIM" if position.upper() == "LEADING" else "RTRIM"
    else:
        raise Unsupported("TRIM with a position or a third argument")
    return name, _node_args(node, "this", "expression")


def _is_whitespace_like(ch: str) -> bool:
    return ch.isspace() or ch in "\x85᠎"


def _strip_spaces(value: str, left: bool, right: bool) -> str:
    """Remove spaces only, and decline when what is left at an edge is another kind of whitespace: whether BigQuery's
    default trims every Unicode white space or only spaces is not something to guess."""

    start, end = 0, len(value)
    if left:
        while start < end and value[start] == " ":
            start += 1
        if start < end and _is_whitespace_like(value[start]):
            raise Unsupported("TRIM of white space other than spaces")
    if right:
        while end > start and value[end - 1] == " ":
            end -= 1
        if end > start and _is_whitespace_like(value[end - 1]):
            raise Unsupported("TRIM of white space other than spaces")
    return value[start:end]


def _trim_set(value, chars, left: bool, right: bool):
    if not chars:
        return value
    start, end = 0, len(value)
    drop = set(chars)
    if left:
        while start < end and value[start] in drop:
            start += 1
    if right:
        while end > start and value[end - 1] in drop:
            end -= 1
    return value[start:end]


def _trim_handler(name: str, left: bool, right: bool):
    @register(name)
    def handler(c, node, args, cx):
        _arity(args, 1, 2, name)
        target, args = _text_args(c, args, name)
        if len(args) == 1:
            if target.kind == "BYTES":
                raise AnalysisError(f"No matching signature for function {name} for argument type: BYTES")
            return E(target, _strict(lambda v: _strip_spaces(v, left, right), args[0]))
        return E(target, _strict(lambda v, chars: _trim_set(v, chars, left, right), *args))

    return handler


_trim_handler("TRIM", True, True)
_trim_handler("LTRIM", True, False)
_trim_handler("RTRIM", False, True)


# --------------------------------------------------------------------------------------------------------------
# LPAD, RPAD, REPLACE, REPEAT, REVERSE
# --------------------------------------------------------------------------------------------------------------


@register_node(exp.Pad)
def _pad_node(node):
    _only(node, "this", "expression", "fill_pattern", "is_left")
    return ("LPAD" if node.args.get("is_left") else "RPAD"), _node_args(node, "this", "expression", "fill_pattern")


def _pad_handler(name: str, left: bool):
    @register(name)
    def handler(c, node, args, cx):
        _arity(args, 2, 3, name)
        texts = [args[0]] + args[2:]
        target, texts = _text_args(c, texts, name)
        value = texts[0]
        size = _int_arg(c, args[1], name)
        space = b" " if target.kind == "BYTES" else " "
        if len(texts) == 1:
            pattern_fn = lambda env: space  # noqa: E731
        else:
            pattern_fn = texts[1].fn
        vf, sf = value.fn, size.fn

        def run(env):
            original, n, pattern = vf(env), sf(env), pattern_fn(env)
            if original is None or n is None or pattern is None:
                return None
            if n < 0:
                raise EvalError("Second argument (output size) for LPAD/RPAD cannot be negative")
            if len(pattern) == 0:
                raise Unsupported("LPAD/RPAD with an empty pattern")
            _check_output(n, target.kind)
            if n <= len(original):
                result = original[:n]
            else:
                need = n - len(original)
                fill = (pattern * (need // len(pattern) + 1))[:need]
                result = fill + original if left else original + fill
            _check_output(0, target.kind, result)
            return result

        return E(target, run)

    return handler


_pad_handler("LPAD", True)
_pad_handler("RPAD", False)


@register_node(exp.Replace)
def _replace_node(node):
    _only(node, "this", "expression", "replacement")
    if _arg(node, "replacement") is None:
        raise Unsupported("REPLACE without a replacement")
    return "REPLACE", _node_args(node, "this", "expression", "replacement")


def _replace(value, old, new):
    if not old:
        return value
    return value.replace(old, new)


@register("REPLACE")
def _replace_fn(c, node, args, cx):
    _arity(args, 3, 3, "REPLACE")
    target, args = _text_args(c, args, "REPLACE")
    return E(target, _strict(_replace, *args))


@register_node(exp.Repeat)
def _repeat_node(node):
    _only(node, "this", "times")
    return "REPEAT", [node.this, node.args["times"]]


@register("REPEAT")
def _repeat_fn(c, node, args, cx):
    _arity(args, 2, 2, "REPEAT")
    target, (value,) = _text_args(c, args[:1], "REPEAT")
    times = _int_arg(c, args[1], "REPEAT")
    kind = target.kind

    def repeat(original, n):
        if n < 0:
            raise EvalError("Second argument (repeat count) for REPEAT cannot be negative")
        if not original or n == 0:
            return original[:0]
        _check_output(len(original) * n, kind)
        result = original * n
        _check_output(0, kind, result)
        return result

    return E(target, _strict(repeat, value, times))


@register_node(exp.Reverse)
def _reverse_node(node):
    _only(node, "this")
    return "REVERSE", [node.this]


@register("REVERSE")
def _reverse_fn(c, node, args, cx):
    _arity(args, 1, 1, "REVERSE")
    target, (a,) = _text_args(c, args, "REVERSE")
    return E(target, _strict(lambda v: v[::-1], a))


# --------------------------------------------------------------------------------------------------------------
# SUBSTR, LEFT, RIGHT, STARTS_WITH, ENDS_WITH
# --------------------------------------------------------------------------------------------------------------


@register_node(exp.Substring)
def _substr_node(node):
    _only(node, "this", "start", "length")
    if _arg(node, "start") is None:
        raise Unsupported("SUBSTR without a position")
    return "SUBSTR", _node_args(node, "this", "start", "length")


def _substr(value, position: int, length: int | None):
    if length is not None and length < 0:
        raise EvalError("Third argument in SUBSTR() cannot be negative")
    size = len(value)
    if position > 0:
        start = min(position - 1, size)
    elif position == 0:
        start = 0
    else:
        start = max(size + position, 0)
    end = size if length is None else min(start + length, size)
    return value[start:end]


@register("SUBSTR", "SUBSTRING")
def _substr_fn(c, node, args, cx):
    _arity(args, 2, 3, "SUBSTR")
    target, (value,) = _text_args(c, args[:1], "SUBSTR")
    position = _int_arg(c, args[1], "SUBSTR")
    if len(args) == 2:
        return E(target, _strict(lambda v, p: _substr(v, p, None), value, position))
    length = _int_arg(c, args[2], "SUBSTR")
    return E(target, _strict(_substr, value, position, length))


@register_node(exp.Left)
def _left_node(node):
    _only(node, "this", "expression")
    return "LEFT", [node.this, node.expression]


@register_node(exp.Right)
def _right_node(node):
    _only(node, "this", "expression")
    return "RIGHT", [node.this, node.expression]


@register("LEFT")
def _left_fn(c, node, args, cx):
    _arity(args, 2, 2, "LEFT")
    target, (value,) = _text_args(c, args[:1], "LEFT")
    length = _int_arg(c, args[1], "LEFT")

    def left(v, n):
        if n < 0:
            raise EvalError("Second argument in LEFT() cannot be negative")
        return v[:n]

    return E(target, _strict(left, value, length))


@register("RIGHT")
def _right_fn(c, node, args, cx):
    _arity(args, 2, 2, "RIGHT")
    target, (value,) = _text_args(c, args[:1], "RIGHT")
    length = _int_arg(c, args[1], "RIGHT")

    def right(v, n):
        if n < 0:
            raise EvalError("Second argument in RIGHT() cannot be negative")
        return v[len(v) - n :] if n < len(v) else v

    return E(target, _strict(right, value, length))


@register_node(exp.StartsWith)
def _starts_node(node):
    _only(node, "this", "expression")
    return "STARTS_WITH", [node.this, node.expression]


@register_node(exp.EndsWith)
def _ends_node(node):
    _only(node, "this", "expression")
    return "ENDS_WITH", [node.this, node.expression]


@register("STARTS_WITH")
def _starts_fn(c, node, args, cx):
    _arity(args, 2, 2, "STARTS_WITH")
    target, args = _text_args(c, args, "STARTS_WITH")
    return E(T.BOOL, _strict(lambda v, p: v.startswith(p), *args))


@register("ENDS_WITH")
def _ends_fn(c, node, args, cx):
    _arity(args, 2, 2, "ENDS_WITH")
    target, args = _text_args(c, args, "ENDS_WITH")
    return E(T.BOOL, _strict(lambda v, p: v.endswith(p), *args))


# --------------------------------------------------------------------------------------------------------------
# STRPOS, INSTR
# --------------------------------------------------------------------------------------------------------------


@register_node(exp.StrPosition)
def _strpos_node(node):
    _only(node, "this", "substr", "position", "occurrence")
    if _arg(node, "occurrence") is not None and _arg(node, "position") is None:
        raise Unsupported("INSTR with an occurrence and no position")
    args = _node_args(node, "this", "substr", "position", "occurrence")
    return ("STRPOS" if len(args) == 2 else "INSTR"), args


def _strpos(value, needle) -> int:
    if not needle:
        raise Unsupported("STRPOS of an empty string (BigQuery's answer is undocumented)")
    return value.find(needle) + 1


@register("STRPOS")
def _strpos_fn(c, node, args, cx):
    _arity(args, 2, 2, "STRPOS")
    target, args = _text_args(c, args[:2], "STRPOS")
    return E(T.INT64, _strict(_strpos, *args))


def _instr(value, needle, position: int = 1, occurrence: int = 1) -> int:
    if position == 0:
        raise EvalError("Position must not be 0")
    if occurrence <= 0:
        raise EvalError("Occurrence must be positive")
    if not needle:
        raise Unsupported("INSTR of an empty string (BigQuery's answer is undocumented)")
    size, n = len(value), len(needle)
    if position > 0:
        start = position - 1
        found = 0
        while True:
            at = value.find(needle, start)
            if at < 0:
                return 0
            found += 1
            if found == occurrence:
                return at + 1
            start = at + 1  # matches may overlap: INSTR("abbba", "bb", 2, 2) is 3
    # Searching backwards from position -k: whether the match must start or must end at or before that position, and
    # whether the next occurrence may overlap, is not documented; answer only when every reading agrees.
    limit = size + position + 1  # the 1-based position the backward search starts from
    if limit < 1:
        raise Unsupported("INSTR with a negative position beyond the start")

    def backward(max_start: int, step: int) -> int:
        at, found = max_start, 0
        while at >= 0:
            at = value.rfind(needle, 0, at + n)
            if at < 0:
                return 0
            found += 1
            if found == occurrence:
                return at + 1
            at -= step
        return 0

    answers = {backward(max_start, step) for max_start in (limit - 1, limit - n) for step in (1, n)}
    if len(answers) != 1:
        raise Unsupported("INSTR with a negative position (readings differ)")
    return answers.pop()


@register("INSTR")
def _instr_fn(c, node, args, cx):
    _arity(args, 2, 4, "INSTR")
    target, texts = _text_args(c, args[:2], "INSTR")
    ints = [_int_arg(c, a, "INSTR") for a in args[2:]]
    return E(T.INT64, _strict(_instr, *texts, *ints))


# --------------------------------------------------------------------------------------------------------------
# SPLIT
# --------------------------------------------------------------------------------------------------------------


@register_node(exp.Split)
def _split_node(node):
    _only(node, "this", "expression")
    return "SPLIT", [node.this, node.expression]


def _split(value, delimiter):
    if not value:
        return (value,)
    if not delimiter:
        if isinstance(value, bytes):
            return tuple(value[i : i + 1] for i in range(len(value)))
        return tuple(value)
    return tuple(value.split(delimiter))


@register("SPLIT")
def _split_fn(c, node, args, cx):
    _arity(args, 1, 2, "SPLIT")
    if len(args) == 1:
        target, texts = _text_args(c, args, "SPLIT")
        if target.kind == "BYTES":
            raise AnalysisError("SPLIT of BYTES needs a delimiter")
        delimiter = E(T.STRING, lambda env: ",")
        texts = [texts[0], delimiter]
    else:
        target, texts = _text_args(c, args, "SPLIT")
    return E(T.array(target), _strict(_split, *texts))


# --------------------------------------------------------------------------------------------------------------
# ASCII, CHR, UNICODE, code points, TRANSLATE
# --------------------------------------------------------------------------------------------------------------


@register_node(exp.Ascii)
def _ascii_node(node):
    _only(node, "this")
    return "ASCII", [node.this]


def _ascii(value) -> int:
    if not value:
        return 0
    first = value[0]
    code = first if isinstance(value, bytes) else ord(first)
    if code > 127:
        raise EvalError("First char of argument of ASCII is out of range [0, 127]")
    return code


@register("ASCII")
def _ascii_fn(c, node, args, cx):
    _arity(args, 1, 1, "ASCII")
    target, (a,) = _text_args(c, args, "ASCII")
    return E(T.INT64, _strict(_ascii, a))


def _valid_code_point(code: int) -> bool:
    return 0 <= code <= 0xD7FF or 0xE000 <= code <= 0x10FFFF


@register_node(exp.Chr)
def _chr_node(node):
    _only(node, "expressions")
    if len(node.expressions) != 1:
        raise Unsupported("CHR with several arguments")
    return "CHR", list(node.expressions)


def _chr(code: int, nul_ok: bool) -> str:
    if code == 0 and not nul_ok:
        raise Unsupported("CHR(0) (BigQuery's documentation says an empty string; the GoogleSQL reference returns NUL)")
    if not _valid_code_point(code):
        raise EvalError(f"Invalid Unicode code point {code}")
    return chr(code)


@register("CHR")
def _chr_fn(c, node, args, cx):
    _arity(args, 1, 1, "CHR")
    a = _int_arg(c, args[0], "CHR")
    nul_ok = c.mode == "googlesql"
    return E(T.STRING, _strict(lambda code: _chr(code, nul_ok), a))


@register_node(exp.Unicode)
def _unicode_node(node):
    _only(node, "this")
    return "UNICODE", [node.this]


@register("UNICODE")
def _unicode_fn(c, node, args, cx):
    _arity(args, 1, 1, "UNICODE")
    a = _string_arg(c, args[0], "UNICODE")
    return E(T.INT64, _strict(lambda v: ord(v[0]) if v else 0, a))


def _array_arg(c, arg: E, elem: T.Type, what: str) -> E:
    if arg.lit == "null":
        return E(T.array(elem), arg.fn, "null", None)
    if arg.type.kind != "ARRAY" or arg.type.elem != elem:
        raise AnalysisError(f"No matching signature for function {what} for argument type: {arg.type}")
    return arg


@register_node(exp.CodePointsToString)
def _cp_string_node(node):
    _only(node, "this")
    return "CODE_POINTS_TO_STRING", [node.this]


@register_node(exp.CodePointsToBytes)
def _cp_bytes_node(node):
    _only(node, "this")
    return "CODE_POINTS_TO_BYTES", [node.this]


def _cp_to_string(values) -> str | None:
    if any(v is None for v in values):
        return None
    out = []
    for code in values:
        if code == 0:
            raise Unsupported("CODE_POINTS_TO_STRING with a 0 code point")
        if not _valid_code_point(code):
            raise EvalError(f"Invalid Unicode code point {code}")
        out.append(chr(code))
    return "".join(out)


def _cp_to_bytes(values) -> bytes | None:
    if any(v is None for v in values):
        return None
    for code in values:
        if not 0 <= code <= 255:
            raise EvalError(f"Invalid byte value {code}")
    return bytes(values)


@register("CODE_POINTS_TO_STRING")
def _cp_string_fn(c, node, args, cx):
    _arity(args, 1, 1, "CODE_POINTS_TO_STRING")
    return E(T.STRING, _strict(_cp_to_string, _array_arg(c, args[0], T.INT64, "CODE_POINTS_TO_STRING")))


@register("CODE_POINTS_TO_BYTES")
def _cp_bytes_fn(c, node, args, cx):
    _arity(args, 1, 1, "CODE_POINTS_TO_BYTES")
    return E(T.BYTES, _strict(_cp_to_bytes, _array_arg(c, args[0], T.INT64, "CODE_POINTS_TO_BYTES")))


@register_node(exp.ToCodePoints)
def _to_cp_node(node):
    _only(node, "this")
    return "TO_CODE_POINTS", [node.this]


@register("TO_CODE_POINTS")
def _to_cp_fn(c, node, args, cx):
    _arity(args, 1, 1, "TO_CODE_POINTS")
    target, (a,) = _text_args(c, args, "TO_CODE_POINTS")
    if target.kind == "BYTES":
        return E(T.array(T.INT64), _strict(lambda v: tuple(v), a))
    return E(T.array(T.INT64), _strict(lambda v: tuple(map(ord, v)), a))


@register_node(exp.Translate)
def _translate_node(node):
    _only(node, "this", "from_", "to")
    return "TRANSLATE", [node.this, node.args["from_"], node.args["to"]]


def _translate(value, source, target):
    is_bytes = isinstance(value, bytes)
    src = list(source) if is_bytes else list(source)
    if len(set(src)) != len(src):
        raise Unsupported("TRANSLATE with a repeated source character (an error in BigQuery's documentation, unverified)")
    mapping = {}
    for i, ch in enumerate(src):
        mapping[ch] = target[i] if i < len(target) else None
    if is_bytes:
        out = bytearray()
        for b in value:
            if b in mapping:
                if mapping[b] is not None:
                    out.append(mapping[b])
            else:
                out.append(b)
        return bytes(out)
    out = []
    for ch in value:
        if ch in mapping:
            if mapping[ch] is not None:
                out.append(mapping[ch])
        else:
            out.append(ch)
    return "".join(out)


@register("TRANSLATE")
def _translate_fn(c, node, args, cx):
    _arity(args, 3, 3, "TRANSLATE")
    target, args = _text_args(c, args, "TRANSLATE")
    return E(target, _strict(_translate, *args))


# --------------------------------------------------------------------------------------------------------------
# hex, base64, base32, bytes to string
# --------------------------------------------------------------------------------------------------------------


@register_node(exp.LowerHex)
def _to_hex_node(node):
    _only(node, "this")
    return "TO_HEX", [node.this]


@register_node(exp.Unhex)
def _from_hex_node(node):
    _only(node, "this")
    return "FROM_HEX", [node.this]


@register_node(exp.ToBase64)
def _to_b64_node(node):
    _only(node, "this")
    return "TO_BASE64", [node.this]


@register_node(exp.FromBase64)
def _from_b64_node(node):
    _only(node, "this")
    return "FROM_BASE64", [node.this]


@register_node(exp.ToBase32)
def _to_b32_node(node):
    _only(node, "this")
    return "TO_BASE32", [node.this]


@register_node(exp.FromBase32)
def _from_b32_node(node):
    _only(node, "this")
    return "FROM_BASE32", [node.this]


@register_node(exp.SafeConvertBytesToString)
def _safe_convert_node(node):
    _only(node, "this")
    return "SAFE_CONVERT_BYTES_TO_STRING", [node.this]


def _bytes_arg(c, arg: E, what: str) -> E:
    if arg.lit == "null":
        return c.coerce(arg, T.BYTES)
    if arg.type != T.BYTES:
        raise AnalysisError(f"No matching signature for function {what} for argument type: {arg.type}")
    return arg


_HEX_DIGITS = frozenset("0123456789abcdefABCDEF")


def _from_hex(text: str) -> bytes:
    if any(ch not in _HEX_DIGITS for ch in text):
        raise EvalError("Failed to decode hex string: invalid character")
    if len(text) % 2:
        text = "0" + text
    return bytes.fromhex(text)


_B64_RE = re.compile(r"[A-Za-z0-9+/]*={0,2}")
_B32_RE = re.compile(r"[A-Z2-7]*={0,6}")


def _from_base64(text: str) -> bytes:
    if not _B64_RE.fullmatch(text) or len(text) % 4:
        raise Unsupported("FROM_BASE64 of input other than padded standard base64")
    try:
        data = base64.b64decode(text, validate=True)
    except (binascii.Error, ValueError):
        raise Unsupported("FROM_BASE64 of malformed input") from None
    if base64.b64encode(data).decode("ascii") != text:
        raise Unsupported("FROM_BASE64 of non-canonical input")
    return data


def _from_base32(text: str) -> bytes:
    if not _B32_RE.fullmatch(text) or len(text) % 8:
        raise Unsupported("FROM_BASE32 of non-canonical input")
    try:
        data = base64.b32decode(text)
    except (binascii.Error, ValueError):
        raise Unsupported("FROM_BASE32 of malformed input") from None
    if base64.b32encode(data).decode("ascii") != text:
        raise Unsupported("FROM_BASE32 of non-canonical input")
    return data


def _replace_invalid_utf8(error):
    if error.end - error.start > 1:
        raise Unsupported("invalid UTF-8 whose replacement granularity is undocumented")
    return "\ufffd", error.end


codecs.register_error("kumosql_gsql_replace", _replace_invalid_utf8)


def _safe_convert(data: bytes) -> str:
    return data.decode("utf-8", errors="kumosql_gsql_replace")


@register("TO_HEX")
def _to_hex_fn(c, node, args, cx):
    _arity(args, 1, 1, "TO_HEX")
    return E(T.STRING, _strict(lambda v: v.hex(), _bytes_arg(c, args[0], "TO_HEX")))


@register("FROM_HEX")
def _from_hex_fn(c, node, args, cx):
    _arity(args, 1, 1, "FROM_HEX")
    return E(T.BYTES, _strict(_from_hex, _string_arg(c, args[0], "FROM_HEX")))


@register("TO_BASE64")
def _to_b64_fn(c, node, args, cx):
    _arity(args, 1, 1, "TO_BASE64")
    return E(T.STRING, _strict(lambda v: base64.b64encode(v).decode("ascii"), _bytes_arg(c, args[0], "TO_BASE64")))


@register("FROM_BASE64")
def _from_b64_fn(c, node, args, cx):
    _arity(args, 1, 1, "FROM_BASE64")
    return E(T.BYTES, _strict(_from_base64, _string_arg(c, args[0], "FROM_BASE64")))


@register("TO_BASE32")
def _to_b32_fn(c, node, args, cx):
    _arity(args, 1, 1, "TO_BASE32")
    return E(T.STRING, _strict(lambda v: base64.b32encode(v).decode("ascii"), _bytes_arg(c, args[0], "TO_BASE32")))


@register("FROM_BASE32")
def _from_b32_fn(c, node, args, cx):
    _arity(args, 1, 1, "FROM_BASE32")
    return E(T.BYTES, _strict(_from_base32, _string_arg(c, args[0], "FROM_BASE32")))


@register("SAFE_CONVERT_BYTES_TO_STRING")
def _safe_convert_fn(c, node, args, cx):
    _arity(args, 1, 1, "SAFE_CONVERT_BYTES_TO_STRING")
    return E(T.STRING, _strict(_safe_convert, _bytes_arg(c, args[0], "SAFE_CONVERT_BYTES_TO_STRING")))


# --------------------------------------------------------------------------------------------------------------
# regular expressions: RE2 syntax translated to Python's ``re``, for the subset both read the same way
# --------------------------------------------------------------------------------------------------------------
#
# Accepted: literals, ``.``, ``[...]`` classes (ranges, negation, ``\d \w \s \D \W`` and single-character escapes),
# ``^ $ \A \z \b \B``, groups ``(...) (?:...) (?P<name>...)``, alternation, greedy and lazy ``* + ? {n} {n,} {n,m}``
# (counts up to 1000), and the escapes ``\n \r \t \f \v \a \xHH`` and punctuation. Leading flags ``(?i) (?m) (?s)``
# are accepted; ``i`` only when pattern and text are ASCII. Everything else (Unicode classes ``\p``, POSIX classes,
# ``\Q..\E``, backreferences, lookaround, possessive quantifiers, ``{`` that is not a repeat, flags anywhere but at
# the start...) raises Unsupported: the two engines disagree on some of those and report errors for others.
#
# The deliberate translations: ``$`` is ``\Z`` (RE2's ``$`` does not match before a final newline), ``\s`` is
# ``[\t\n\f\r ]`` (RE2's has no vertical tab), ``\z`` is ``\Z``, and ``re.ASCII`` makes ``\w \d \b`` ASCII-only as in RE2.
# A pattern that can match the empty string is refused where the engines differ on empty matches (after a non-empty
# match, at the end of the text).

_MAX_REPEAT = 1000
_SIMPLE_ESCAPES = {"n": "\n", "r": "\r", "t": "\t", "f": "\f", "v": "\v", "a": "\a"}
_HEXDIGITS = "0123456789abcdefABCDEF"


class _Compiled:
    __slots__ = ("regex", "groups", "nullable", "context", "ignorecase", "empty", "not_boundary")

    def __init__(self, regex, groups, nullable, context, ignorecase, empty, not_boundary):
        self.not_boundary = not_boundary
        self.regex = regex
        self.groups = groups
        self.nullable = nullable
        self.context = context
        self.ignorecase = ignorecase
        self.empty = empty


def _py_literal(ch: str) -> str:
    return re.escape(ch)


def _py_class_char(ch: str) -> str:
    code = ord(ch)
    if code < 0x20 or code == 0x7F:
        return "\\x%02x" % code
    if ch.isalnum() or code >= 0x80 or ch == " ":
        return ch
    return "\\" + ch


class _RegexParser:
    def __init__(self, pattern: str):
        self.p = pattern
        self.i = 0
        self.groups = 0
        self.names: set = set()
        self.multiline = False
        self.dotall = False
        self.ignorecase = False
        self.context = False
        self.not_boundary = False

    def fail(self, why: str):
        raise Unsupported(f"regular expression construct outside the RE2/Python common subset: {why}")

    def invalid(self, why: str):
        """A pattern RE2 certainly rejects (``Cannot parse regular expression``): a runtime error, as in BigQuery."""

        raise EvalError(f"Cannot parse regular expression: {why}")

    def parse(self):
        p = self.p
        # leading flag groups
        while p.startswith("(?", self.i):
            j = self.i + 2
            k = j
            while k < len(p) and p[k] in "ims":
                k += 1
            if k > j and k < len(p) and p[k] == ")":
                for flag in p[j:k]:
                    if flag == "i":
                        self.ignorecase = True
                    elif flag == "m":
                        self.multiline = True
                    else:
                        self.dotall = True
                self.i = k + 1
            else:
                break
        out, nullable, _ = self.alternation(0)
        if self.i != len(p):
            self.invalid("unbalanced parenthesis")
        return out, nullable

    # alternation -> (python text, nullable, repeat product)
    def alternation(self, depth: int):
        branches = []
        nullable = False
        product = 1
        while True:
            text, null, prod = self.sequence(depth)
            branches.append(text)
            nullable = nullable or null
            product = max(product, prod)
            if self.i < len(self.p) and self.p[self.i] == "|":
                self.i += 1
                continue
            break
        return "|".join(branches), nullable, product

    def sequence(self, depth: int):
        items = []
        nullable = True
        product = 1
        p = self.p
        while self.i < len(p) and p[self.i] not in "|" and not (p[self.i] == ")" and depth > 0):
            text, atom_null, zero_width, prod = self.atom(depth)
            quant = self.quantifier()
            if quant is not None:
                qtext, lo, hi = quant
                if zero_width:
                    self.fail("repetition of an assertion")
                if atom_null:
                    self.fail("repetition of a group that can match the empty string")
                text += qtext
                if hi is not None and hi > 1:
                    prod *= hi
                elif hi is None and lo > 1:
                    prod *= lo
                if prod > _MAX_REPEAT:
                    self.fail("nested repetition counts beyond 1000")
                atom_null = lo == 0
            product = max(product, prod)
            items.append(text)
            nullable = nullable and atom_null
        return "".join(items), nullable, product

    def quantifier(self):
        p = self.p
        if self.i >= len(p):
            return None
        ch = p[self.i]
        if ch in "*+?":
            self.i += 1
            lo, hi = {"*": (0, None), "+": (1, None), "?": (0, 1)}[ch]
            text = ch
        elif ch == "{":
            m = re.compile(r"\{(\d+)(?:(,)(\d*))?\}").match(p, self.i)
            if not m:
                self.fail("a '{' that is not a repeat")
            lo = int(m.group(1))
            if m.group(2):
                hi = int(m.group(3)) if m.group(3) else None
            else:
                hi = lo
            if lo > _MAX_REPEAT or (hi is not None and hi > _MAX_REPEAT) or (hi is not None and hi < lo):
                self.invalid("bad repetition operator")
            self.i = m.end()
            text = m.group(0)
        else:
            return None
        if self.i < len(p) and p[self.i] == "?":
            self.i += 1
            text += "?"
        if self.i < len(p) and (p[self.i] in "*+?" or p[self.i] == "{"):
            self.fail("stacked repetition operators")
        return text, lo, hi

    def atom(self, depth: int):
        p = self.p
        ch = p[self.i]
        if ch == "(":
            return self.group(depth)
        if ch == "[":
            return self.char_class(), False, False, 1
        if ch == ".":
            self.i += 1
            return ".", False, False, 1
        if ch == "^":
            self.i += 1
            self.context = True
            return "^", True, True, 1
        if ch == "$":
            self.i += 1
            return ("$" if self.multiline else r"\Z"), True, True, 1
        if ch == "\\":
            return self.escape()
        if ch in "*+?":
            self.invalid("missing argument to repetition operator")
        if ch == "{":
            self.fail("a '{' that is not a repeat")
        if ch == ")":
            self.invalid("unbalanced parenthesis")
        self.i += 1
        if self.ignorecase and not ch.isascii():
            self.fail("case-insensitive match of a non-ASCII pattern")
        return _py_literal(ch), False, False, 1

    def group(self, depth: int):
        p = self.p
        self.i += 1
        if p.startswith("?", self.i):
            if p.startswith("?:", self.i):
                self.i += 2
                prefix = "(?:"
            elif p.startswith("?P<", self.i):
                m = re.compile(r"\?P<([A-Za-z_][A-Za-z0-9_]*)>").match(p, self.i)
                if not m or m.group(1) in self.names:
                    self.fail("group name")
                self.names.add(m.group(1))
                self.groups += 1
                self.i = m.end()
                prefix = "(?P<%s>" % m.group(1)
            else:
                self.fail("(? group")
        else:
            self.groups += 1
            prefix = "("
        text, nullable, product = self.alternation(depth + 1)
        if self.i >= len(p) or p[self.i] != ")":
            self.invalid("unbalanced parenthesis")
        self.i += 1
        return prefix + text + ")", nullable, False, product

    def escape(self):
        p = self.p
        if self.i + 1 >= len(p):
            self.invalid("trailing backslash")
        ch = p[self.i + 1]
        self.i += 2
        if ch in "dDwW":
            return "\\" + ch, False, False, 1
        if ch == "s":
            return r"[\t\n\f\r ]", False, False, 1
        if ch == "S":
            return r"[^\t\n\f\r ]", False, False, 1
        if ch == "b":
            self.context = True
            return r"\b", True, True, 1
        if ch == "B":
            # Python's \B never matches the empty string; RE2's does
            self.context = True
            self.not_boundary = True
            return r"(?:(?<=\w)(?=\w)|(?<!\w)(?!\w))", True, True, 1
        if ch == "A":
            self.context = True
            return r"\A", True, True, 1
        if ch == "z":
            return r"\Z", True, True, 1
        if ch in _SIMPLE_ESCAPES:
            return _py_literal(_SIMPLE_ESCAPES[ch]), False, False, 1
        if ch == "x":
            code = self.hex_byte()
            return _py_literal(chr(code)), False, False, 1
        if ch.isascii() and not (ch.isalnum()):
            return _py_literal(ch), False, False, 1
        self.fail(f"escape \\{ch}")

    def hex_byte(self) -> int:
        h = self.p[self.i : self.i + 2]
        if len(h) != 2 or any(c not in _HEXDIGITS for c in h):
            self.fail("\\x escape")
        self.i += 2
        return int(h, 16)

    def class_escape(self):
        """An escape inside ``[...]``: ``("set", text)`` for a class, ``("char", ch)`` for one character."""

        p = self.p
        if self.i + 1 >= len(p):
            self.invalid("trailing backslash")
        ch = p[self.i + 1]
        self.i += 2
        if ch in "dDwW":
            return "set", "\\" + ch
        if ch == "s":
            return "set", r"\t\n\f\r "
        if ch in _SIMPLE_ESCAPES:
            return "char", _SIMPLE_ESCAPES[ch]
        if ch == "x":
            return "char", chr(self.hex_byte())
        if ch.isascii() and not ch.isalnum():
            return "char", ch
        self.fail(f"escape \\{ch} inside a class")

    def char_class(self) -> str:
        p = self.p
        self.i += 1
        negate = False
        if p.startswith("^", self.i):
            negate = True
            self.i += 1
        items = []  # ("char", ch) | ("set", text) | ("range", lo, hi)
        first = True
        while True:
            if self.i >= len(p):
                self.invalid("missing closing ]")
            ch = p[self.i]
            if ch == "]" and not first:
                self.i += 1
                break
            first = False
            if ch == "[":
                self.fail("'[' inside a class")
            if ch == "\\":
                kind, value = self.class_escape()
            else:
                self.i += 1
                kind, value = "char", ch
            if kind == "char" and self.ignorecase and not value.isascii():
                self.fail("case-insensitive match of a non-ASCII class")
            if (
                kind == "char"
                and self.i + 1 < len(p)
                and p[self.i] == "-"
                and p[self.i + 1] != "]"
            ):
                self.i += 1
                end = p[self.i]
                if end == "[":
                    self.fail("'[' inside a class")
                if end == "\\":
                    kind2, value2 = self.class_escape()
                    if kind2 != "char":
                        self.fail("class as the end of a range")
                else:
                    self.i += 1
                    value2 = end
                if ord(value2) < ord(value):
                    self.invalid("invalid character class range")
                if self.i < len(p) and p[self.i] == "-" and p[self.i + 1 : self.i + 2] != "]":
                    self.fail("range followed by '-'")
                items.append(("range", value, value2))
                continue
            items.append((kind, value))
        body = []
        for item in items:
            if item[0] == "char":
                body.append(_py_class_char(item[1]))
            elif item[0] == "set":
                body.append(item[1])
            else:
                body.append(_py_class_char(item[1]) + "-" + _py_class_char(item[2]))
        return "[" + ("^" if negate else "") + "".join(body) + "]"


@lru_cache(maxsize=512)
def _compile_re2(pattern: str) -> _Compiled:
    parser = _RegexParser(pattern)
    text, nullable = parser.parse()
    flags = re.ASCII
    if parser.multiline:
        flags |= re.MULTILINE
    if parser.dotall:
        flags |= re.DOTALL
    if parser.ignorecase:
        flags |= re.IGNORECASE
    try:
        regex = re.compile(text, flags)
    except (re.error, RecursionError, OverflowError):
        raise Unsupported("regular expression Python rejects") from None
    if regex.groups != parser.groups:
        raise Unsupported("regular expression group count mismatch")
    return _Compiled(regex, parser.groups, nullable, parser.context, parser.ignorecase, pattern == "", parser.not_boundary)


def _regex_for(pattern, text) -> tuple:
    """``(compiled, text')``: both as ``str`` (BYTES by their Latin-1 code points, one per byte)."""

    is_bytes = isinstance(pattern, bytes)
    pat = pattern.decode("latin-1") if is_bytes else pattern
    value = text.decode("latin-1") if is_bytes else text
    compiled = _compile_re2(pat)
    if compiled.ignorecase and not value.isascii():
        raise Unsupported("case-insensitive regular expression on a non-ASCII value")
    if compiled.not_boundary and not is_bytes and not value.isascii():
        # RE2 scans bytes: \B can match in the middle of a multi-byte character, which Python never does
        raise Unsupported("\\B on a non-ASCII value")
    return compiled, value


def _out(value: str | None, as_bytes: bool):
    if value is None or not as_bytes:
        return value
    return value.encode("latin-1")


def _capture_group(compiled: _Compiled) -> int:
    if compiled.groups > 1:
        raise EvalError("Regular expressions passed into extraction functions must not have more than 1 capturing group")
    return 1 if compiled.groups == 1 else 0


@register_node(exp.RegexpLike)
def _regexp_like_node(node):
    _only(node, "this", "expression")
    return "REGEXP_CONTAINS", [node.this, node.expression]


@register("REGEXP_CONTAINS")
def _regexp_contains_fn(c, node, args, cx):
    _arity(args, 2, 2, "REGEXP_CONTAINS")
    target, args = _text_args(c, args, "REGEXP_CONTAINS")

    def contains(value, pattern):
        compiled, text = _regex_for(pattern, value)
        return compiled.regex.search(text) is not None

    return E(T.BOOL, _strict(contains, *args))


@register_node(exp.RegexpExtract)
def _regexp_extract_node(node):
    _only(node, "this", "expression", "position", "occurrence", "group", "null_if_pos_overflow")
    group = _arg(node, "group")
    if group is not None and not (isinstance(group, exp.Literal) and not group.is_string and group.name in ("0", "1")):
        raise Unsupported("REGEXP_EXTRACT with a group argument")
    if _arg(node, "occurrence") is not None and _arg(node, "position") is None:
        raise Unsupported("REGEXP_EXTRACT with an occurrence and no position")
    return "REGEXP_EXTRACT", _node_args(node, "this", "expression", "position", "occurrence")


def _search_from(compiled: _Compiled, text: str, position: int, occurrence: int, what: str):
    """The ``occurrence``-th match searching from the 1-based ``position``, or None."""

    if position < 1 or occurrence < 1:
        raise Unsupported(f"{what} with a non-positive position or occurrence")
    if position > len(text) and not (position == 1 and not text):
        raise Unsupported(f"{what} with a position beyond the value")
    if (position > 1 or occurrence > 1) and compiled.context:
        raise Unsupported(f"{what} resumed in the middle of a value with an anchor or word boundary in the pattern")
    if occurrence > 1 and compiled.nullable:
        raise Unsupported(f"{what} occurrence of a pattern that can match the empty string")
    start = position - 1
    match = None
    for _ in range(occurrence):
        match = compiled.regex.search(text, start)
        if match is None:
            return None
        start = match.end()
    return match


@register("REGEXP_EXTRACT", "REGEXP_SUBSTR")
def _regexp_extract_fn(c, node, args, cx):
    _arity(args, 2, 4, "REGEXP_EXTRACT")
    target, texts = _text_args(c, args[:2], "REGEXP_EXTRACT")
    ints = [_int_arg(c, a, "REGEXP_EXTRACT") for a in args[2:]]
    as_bytes = target.kind == "BYTES"

    def extract(value, pattern, position=1, occurrence=1):
        compiled, text = _regex_for(pattern, value)
        group = _capture_group(compiled)
        match = _search_from(compiled, text, position, occurrence, "REGEXP_EXTRACT")
        if match is None:
            return None
        return _out(match.group(group), as_bytes)

    return E(target, _strict(extract, *texts, *ints))


@register_node(exp.RegexpExtractAll)
def _regexp_extract_all_node(node):
    _only(node, "this", "expression", "group")
    group = _arg(node, "group")
    if group is not None and not (isinstance(group, exp.Literal) and not group.is_string and group.name in ("0", "1")):
        raise Unsupported("REGEXP_EXTRACT_ALL with a group argument")
    return "REGEXP_EXTRACT_ALL", [node.this, node.expression]


@register("REGEXP_EXTRACT_ALL")
def _regexp_extract_all_fn(c, node, args, cx):
    _arity(args, 2, 2, "REGEXP_EXTRACT_ALL")
    target, texts = _text_args(c, args, "REGEXP_EXTRACT_ALL")
    as_bytes = target.kind == "BYTES"

    def extract_all(value, pattern):
        compiled, text = _regex_for(pattern, value)
        group = _capture_group(compiled)
        if compiled.empty:
            if not text:
                raise Unsupported("REGEXP_EXTRACT_ALL of an empty pattern on an empty value")
            return tuple(_out("", as_bytes) for _ in text)
        if compiled.nullable:
            raise Unsupported("REGEXP_EXTRACT_ALL with a pattern that can match the empty string")
        if compiled.context:
            raise Unsupported("REGEXP_EXTRACT_ALL with an anchor or word boundary in the pattern")
        out = []
        for match in compiled.regex.finditer(text):
            piece = match.group(group)
            if piece is None:
                raise Unsupported("REGEXP_EXTRACT_ALL with a group that did not take part in a match")
            out.append(_out(piece, as_bytes))
        return tuple(out)

    return E(T.array(target), _strict(extract_all, *texts))


@register_node(exp.RegexpReplace)
def _regexp_replace_node(node):
    _only(node, "this", "expression", "replacement")
    if _arg(node, "replacement") is None:
        raise Unsupported("REGEXP_REPLACE without a replacement")
    return "REGEXP_REPLACE", [node.this, node.expression, node.args["replacement"]]


def _parse_replacement(template: str, groups: int) -> list:
    parts: list = []
    buf = []
    i = 0
    while i < len(template):
        ch = template[i]
        if ch != "\\":
            buf.append(ch)
            i += 1
            continue
        nxt = template[i + 1 : i + 2]
        if nxt == "\\":
            buf.append("\\")
        elif nxt.isdigit() and nxt.isascii():
            if int(nxt) > groups:
                raise Unsupported("REGEXP_REPLACE referring to a group the pattern lacks")
            if buf:
                parts.append("".join(buf))
                buf = []
            parts.append(int(nxt))
        else:
            raise Unsupported("REGEXP_REPLACE with a backslash that is not \\\\ or \\digit")
        i += 2
    if buf:
        parts.append("".join(buf))
    return parts


@register("REGEXP_REPLACE")
def _regexp_replace_fn(c, node, args, cx):
    _arity(args, 3, 3, "REGEXP_REPLACE")
    target, texts = _text_args(c, args, "REGEXP_REPLACE")
    as_bytes = target.kind == "BYTES"

    def replace(value, pattern, replacement):
        compiled, text = _regex_for(pattern, value)
        template = replacement.decode("latin-1") if as_bytes else replacement
        parts = _parse_replacement(template, compiled.groups)
        if compiled.empty:
            rep = "".join(p if isinstance(p, str) else "" for p in parts) if all(
                isinstance(p, str) or p == 0 for p in parts
            ) else None
            if rep is None:
                raise Unsupported("REGEXP_REPLACE of an empty pattern referring to a group")
            result = rep + "".join(ch + rep for ch in text)
            return _out(result, as_bytes)
        if compiled.nullable:
            raise Unsupported("REGEXP_REPLACE with a pattern that can match the empty string")
        if compiled.context:
            raise Unsupported("REGEXP_REPLACE with an anchor or word boundary in the pattern")
        out = []
        last = 0
        for match in compiled.regex.finditer(text):
            out.append(text[last : match.start()])
            for part in parts:
                out.append(part if isinstance(part, str) else (match.group(part) or ""))
            last = match.end()
        out.append(text[last:])
        return _out("".join(out), as_bytes)

    return E(target, _strict(replace, *texts))


@register_node(exp.RegexpInstr)
def _regexp_instr_node(node):
    _only(node, "this", "expression", "position", "occurrence", "option")
    if _arg(node, "occurrence") is not None and _arg(node, "position") is None:
        raise Unsupported("REGEXP_INSTR with an occurrence and no position")
    if _arg(node, "option") is not None and _arg(node, "occurrence") is None:
        raise Unsupported("REGEXP_INSTR with an option and no occurrence")
    return "REGEXP_INSTR", _node_args(node, "this", "expression", "position", "occurrence", "option")


@register("REGEXP_INSTR")
def _regexp_instr_fn(c, node, args, cx):
    _arity(args, 2, 5, "REGEXP_INSTR")
    target, texts = _text_args(c, args[:2], "REGEXP_INSTR")
    ints = [_int_arg(c, a, "REGEXP_INSTR") for a in args[2:]]

    def instr(value, pattern, position=1, occurrence=1, option=0):
        compiled, text = _regex_for(pattern, value)
        group = _capture_group(compiled)
        if option not in (0, 1):
            raise Unsupported("REGEXP_INSTR with an option other than 0 or 1")
        if compiled.empty and text and 1 <= position <= len(text) and occurrence >= 1:
            return 0  # every match is empty and the reference reports none: REGEXP_INSTR("-2020-jack", "", 2) is 0
        if compiled.nullable:
            raise Unsupported("REGEXP_INSTR with a pattern that can match the empty string")
        match = _search_from(compiled, text, position, occurrence, "REGEXP_INSTR")
        if match is None:
            return 0
        if match.group(group) is None:
            raise Unsupported("REGEXP_INSTR with a group that did not take part in the match")
        return (match.end(group) if option else match.start(group)) + 1

    return E(T.INT64, _strict(instr, *texts, *ints))


# --------------------------------------------------------------------------------------------------------------
# FORMAT
# --------------------------------------------------------------------------------------------------------------
#
# Implemented: ``%[flags][width][.precision]specifier`` with flags ``- + space # 0``, ``*`` for width and precision,
# specifiers ``d i x X o`` on INT64, ``f F e E g G`` on FLOAT64 (Python's ``%`` formatting is C printf's for finite
# values), ``s t T`` on STRING and ``t T`` on INT64/BOOL (``t`` on finite FLOAT64), and ``%%``. Everything else
# (``'``, ``%u %p %P %b``, NUMERIC and BIGNUMERIC, dates, arrays, structs, NULL arguments except as a ``*`` operand or
# under ``*``, negative widths) raises Unsupported when met.


class _Spec:
    __slots__ = ("flags", "width", "prec", "conv")

    def __init__(self, flags, width, prec, conv):
        self.flags, self.width, self.prec, self.conv = flags, width, prec, conv


@lru_cache(maxsize=512)
def _parse_format(template: str) -> tuple:
    parts: list = []
    buf: list = []
    i, n = 0, len(template)
    while i < n:
        ch = template[i]
        if ch != "%":
            buf.append(ch)
            i += 1
            continue
        i += 1
        j = i
        while j < n and template[j] in "-+ #0'":
            j += 1
        flags = template[i:j]
        k = j
        if k < n and template[k] == "*":
            width, k = "*", k + 1
        else:
            while k < n and template[k].isdigit() and template[k].isascii():
                k += 1
            width = int(template[j:k]) if k > j else None
        prec = None
        if k < n and template[k] == ".":
            k += 1
            if k < n and template[k] == "*":
                prec, k = "*", k + 1
            else:
                m = k
                while k < n and template[k].isdigit() and template[k].isascii():
                    k += 1
                prec = int(template[m:k]) if k > m else 0
        if k >= n:
            raise Unsupported("FORMAT pattern ending inside a specifier")
        conv = template[k]
        i = k + 1
        if "'" in flags:
            raise Unsupported("FORMAT flag '")
        if conv == "%":
            if flags or width is not None or prec is not None:
                raise Unsupported("FORMAT %% with flags")
            buf.append("%")
            continue
        if conv not in "dixXofFeEgGstT":
            raise Unsupported(f"FORMAT specifier %{conv}")
        if buf:
            parts.append("".join(buf))
            buf = []
        parts.append(_Spec(flags, width, prec, conv))
    if buf:
        parts.append("".join(buf))
    return tuple(parts)


def _pad(text: str, width: int | None, left: bool) -> str:
    if width is None or len(text) >= width:
        return text
    return text.ljust(width) if left else text.rjust(width)


def _format_int(spec: _Spec, width, prec, value: int) -> str:
    flags = spec.flags
    if spec.conv in "di":
        if "#" in flags:
            raise Unsupported("FORMAT # flag with %d")
        digits = str(abs(value))
        if prec is not None:
            if prec == 0 and value == 0:
                raise Unsupported("FORMAT %.0d of zero")
            digits = digits.rjust(prec, "0")
        sign = "-" if value < 0 else ("+" if "+" in flags else (" " if " " in flags else ""))
        prefix = ""
    else:
        if value < 0:
            raise Unsupported("FORMAT of a negative integer with %x %X %o")
        if "+" in flags or " " in flags:
            raise Unsupported("FORMAT sign flags with %x %X %o")
        digits = format(value, {"x": "x", "X": "X", "o": "o"}[spec.conv])
        if prec is not None:
            if prec == 0 and value == 0:
                raise Unsupported("FORMAT %.0x of zero")
            digits = digits.rjust(prec, "0")
        sign = ""
        prefix = ""
        if "#" in flags:
            if spec.conv == "o":
                if not digits.startswith("0"):
                    digits = "0" + digits
            elif value != 0:
                prefix = "0" + spec.conv
    body = sign + prefix + digits
    if width is not None and len(body) < width:
        if "-" in flags:
            body = body.ljust(width)
        elif "0" in flags and prec is None:
            body = sign + prefix + digits.rjust(width - len(sign) - len(prefix), "0")
        else:
            body = body.rjust(width)
    return body


def _format_float(spec: _Spec, width, prec, value: float) -> str:
    flags = spec.flags
    if value != value or value in (float("inf"), float("-inf")):
        if flags or width is not None or prec is not None:
            raise Unsupported("FORMAT of a non-finite float with flags, width or precision")
        return ("%" + spec.conv) % value
    fmt = "%" + flags + (str(width) if width is not None else "") + ("." + str(prec) if prec is not None else "") + spec.conv
    return fmt % value


_QUOTE_OK = frozenset(chr(c) for c in range(0x20, 0x7F)) - set("\"'`?")


def _sql_string_literal(value: str) -> str:
    out = []
    for ch in value:
        if ch == "\\":
            out.append("\\\\")
        elif ch == "\n":
            out.append("\\n")
        elif ch == "\r":
            out.append("\\r")
        elif ch == "\t":
            out.append("\\t")
        elif ch in _QUOTE_OK or (not ch.isascii() and ch.isprintable()):
            out.append(ch)
        else:
            raise Unsupported("FORMAT %T of a string with quotes, control or non-printable characters")
    return '"' + "".join(out) + '"'


def _format_text(spec: _Spec, width, prec, kind: str, value) -> str:
    conv = spec.conv
    if kind == "STRING":
        text = value if conv in "st" else _sql_string_literal(value)
    elif kind == "INT64" and conv in "tT":
        text = str(value)
    elif kind == "BOOL" and conv in "tT":
        text = "true" if value else "false"
    elif kind == "FLOAT64" and conv == "t" and value == value and value not in (float("inf"), float("-inf")):
        text = V.format_double(value)
    else:
        raise Unsupported(f"FORMAT %{conv} of {kind}")
    if set(spec.flags) - {"-"}:
        raise Unsupported("FORMAT flags other than - on %s %t %T")
    if (width is not None or prec is not None) and not text.isascii():
        raise Unsupported("FORMAT width or precision on non-ASCII text")
    if prec is not None:
        text = text[:prec]
    return _pad(text, width, "-" in spec.flags)


_NOT_INTEGER = ("STRING", "BYTES", "BOOL")


def _format(types: list, template: str, values: list):
    parts = _parse_format(template)
    out: list = []
    index = 0

    def take():
        nonlocal index
        if index >= len(values):
            raise EvalError(f'Too few arguments to FORMAT for pattern "{template}"; Expected {index + 2}; Got {len(values) + 1}')
        index += 1
        return index - 1

    for part in parts:
        if isinstance(part, str):
            out.append(part)
            continue
        starred = part.width == "*" or part.prec == "*"
        resolved = []
        for operand in (part.width, part.prec):
            if operand == "*":
                i = take()
                if values[i] is None:
                    return None
                if types[i].kind != "INT64":
                    raise EvalError("Invalid type for the * argument of FORMAT; Expected integer")
                if values[i] < 0:
                    raise Unsupported("FORMAT with a negative * operand")
                resolved.append(values[i])
            else:
                resolved.append(operand)
        width, prec = resolved
        i = take()
        kind, value = types[i].kind, values[i]
        if value is None:
            if starred and part.conv in "dixXofFeEgG":
                return None
            raise Unsupported("FORMAT with a NULL argument")
        if part.conv in "dixXo":
            if kind != "INT64":
                if kind in _NOT_INTEGER:
                    raise EvalError(f"Invalid type for argument {i + 2} to FORMAT; Expected integer; Got {kind}")
                raise Unsupported(f"FORMAT %{part.conv} of {kind}")
            out.append(_format_int(part, width, prec, value))
        elif part.conv in "fFeEgG":
            if kind != "FLOAT64":
                if kind in _NOT_INTEGER:
                    raise EvalError(f"Invalid type for argument {i + 2} to FORMAT; Expected floating point; Got {kind}")
                raise Unsupported(f"FORMAT %{part.conv} of {kind}")
            out.append(_format_float(part, width, prec, value))
        else:
            out.append(_format_text(part, width, prec, kind, value))
    if index != len(values):
        raise EvalError(f'Too many arguments to FORMAT for pattern "{template}"; Expected {index + 1}; Got {len(values) + 1}')
    return "".join(out)


@register_node(exp.Format)
def _format_node(node):
    _only(node, "this", "expressions")
    return "FORMAT", [node.this] + list(node.expressions)


@register("FORMAT")
def _format_fn(c, node, args, cx):
    if not args:
        raise AnalysisError("No matching signature for function FORMAT with no arguments")
    template = _string_arg(c, args[0], "FORMAT")
    values = args[1:]
    types = [v.type for v in values]
    tf = template.fn
    fns = [v.fn for v in values]

    def run(env):
        text = tf(env)
        vals = [f(env) for f in fns]
        if text is None:
            return None
        return _format(types, text, vals)

    return E(T.STRING, run)


# --------------------------------------------------------------------------------------------------------------
# SOUNDEX, EDIT_DISTANCE, NORMALIZE
# --------------------------------------------------------------------------------------------------------------

_SOUNDEX = {}
for _letters, _digit in (("BFPV", "1"), ("CGJKQSXZ", "2"), ("DT", "3"), ("L", "4"), ("MN", "5"), ("R", "6")):
    for _ch in _letters:
        _SOUNDEX[_ch] = _digit


def _soundex_once(value: str, carry_first: bool, other_sep: bool, w_sep: bool, y_sep: bool) -> str:
    out = ""
    prev = ""
    for ch in value:
        letter = ch.upper() if ch.isascii() and ch.isalpha() else None
        if letter is None:
            if other_sep and out:
                prev = ""
            continue
        code = _SOUNDEX.get(letter)
        if not out:
            out = letter
            prev = code if (carry_first and code) else ""
            continue
        if letter in "HW":
            if (letter == "W" and w_sep) or (letter == "H" and False):
                prev = ""
            continue
        if letter == "Y" and y_sep:
            prev = ""
            continue
        if code is None:
            prev = ""  # a vowel (or Y read as one) separates repeated codes
            continue
        if code != prev:
            out += code
        prev = code
        if len(out) == 4:
            break
    return (out + "000")[:4] if out else ""


def _soundex(value: str) -> str:
    if any(not ch.isascii() and ch.isalpha() for ch in value):
        raise Unsupported("SOUNDEX of a non-ASCII letter")
    answers = {
        _soundex_once(value, a, b, c, d)
        for a in (True, False)
        for b in (True, False)
        for c in (True, False)
        for d in (True, False)
    }
    if len(answers) != 1:
        raise Unsupported("SOUNDEX where undocumented rules (separators, Y, W) decide the code")
    return answers.pop()


@register_node(exp.Soundex)
def _soundex_node(node):
    _only(node, "this")
    return "SOUNDEX", [node.this]


@register("SOUNDEX")
def _soundex_fn(c, node, args, cx):
    _arity(args, 1, 1, "SOUNDEX")
    return E(T.STRING, _strict(_soundex, _string_arg(c, args[0], "SOUNDEX")))


@register_node(exp.Levenshtein)
def _levenshtein_node(node):
    _only(node, "this", "expression", "max_dist")
    return "EDIT_DISTANCE", _node_args(node, "this", "expression", "max_dist")


def _edit_distance(a, b, max_distance: int | None = None) -> int:
    if max_distance is not None and max_distance < 0:
        raise EvalError("max_distance must not be negative")
    if isinstance(a, str) and not (a.isascii() and b.isascii()):
        raise Unsupported("EDIT_DISTANCE of non-ASCII text (characters or bytes is undocumented)")
    if len(a) < len(b):
        a, b = b, a
    previous = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        current = [i]
        for j, cb in enumerate(b, 1):
            current.append(min(previous[j] + 1, current[j - 1] + 1, previous[j - 1] + (ca != cb)))
        previous = current
    distance = previous[-1]
    return distance if max_distance is None else min(distance, max_distance)


@register("EDIT_DISTANCE")
def _edit_distance_fn(c, node, args, cx):
    _arity(args, 2, 3, "EDIT_DISTANCE")
    target, texts = _text_args(c, args[:2], "EDIT_DISTANCE")
    if len(args) == 3:
        if args[2].lit == "null":
            raise Unsupported("EDIT_DISTANCE with a NULL max_distance")
        limit = _int_arg(c, args[2], "EDIT_DISTANCE")
        return E(T.INT64, _strict(_edit_distance, *texts, limit))
    return E(T.INT64, _strict(_edit_distance, *texts))


_FORMS = ("NFC", "NFKC", "NFD", "NFKD")


@register_node(exp.Normalize)
def _normalize_node(node):
    _only(node, "this", "form", "is_casefold")
    form = _arg(node, "form")
    if form is None:
        name = "NFC"
    elif isinstance(form, (exp.Var, exp.Column)) and form.name.upper() in _FORMS:
        name = form.name.upper()
    else:
        raise Unsupported("NORMALIZE mode")
    return "NORMALIZE_" + name + ("_CASEFOLD" if node.args.get("is_casefold") else ""), [node.this]


def _assigned(value: str) -> None:
    if value.isascii():
        return
    for ch in value:
        if unicodedata.category(ch) == "Cn":
            raise Unsupported("normalization of a character unassigned in this Unicode version")


def _normalizer(form: str, casefold: bool):
    def normalize(value: str) -> str:
        _assigned(value)
        if not casefold:
            return unicodedata.normalize(form, value)
        # the order of normalization and case folding is not documented: answer when every order agrees
        a = unicodedata.normalize(form, unicodedata.normalize(form, value).casefold())
        b = unicodedata.normalize(form, value.casefold())
        d = unicodedata.normalize(form, value).casefold()
        if not (a == b == d):
            raise Unsupported("NORMALIZE_AND_CASEFOLD where the order of folding and normalizing matters")
        return a

    return normalize


def _register_normalize(form: str, casefold: bool) -> None:
    name = "NORMALIZE_" + form + ("_CASEFOLD" if casefold else "")
    fn = _normalizer(form, casefold)

    @register(name)
    def handler(c, node, args, cx):
        _arity(args, 1, 1, name)
        return E(T.STRING, _strict(fn, _string_arg(c, args[0], name)))


for _form in _FORMS:
    _register_normalize(_form, False)
    _register_normalize(_form, True)

# COLLATE and CONTAINS_SUBSTR are not registered: the first needs collation support, and sqlglot reads the second as
# ``LOWER(a) CONTAINS LOWER(b)``, which is not what BigQuery computes.
