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


def _node_args(node: exp.Expression, *keys: str) -> list:
    """The argument nodes for ``keys`` in order, skipping the absent ones."""

    out = []
    for key in keys:
        value = node.args.get(key)
        if value is not None:
            out.append(value)
    return out


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


def _chars(value) -> int:
    return len(value)


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
    for a in args:
        if a.lit != "null" and a.type.kind not in _TEXT:
            # BigQuery's CONCAT takes only STRING or BYTES; the GoogleSQL reference accepts other types under a
            # language feature BigQuery lacks, so the answer for this call is not the reference's: decline.
            raise Unsupported("CONCAT of a non-string argument")
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


def _case_safe(value: str) -> None:
    if value.isascii():
        return
    for ch in value:
        if not ch.isascii() and not _caseless(ch):
            raise Unsupported("case mapping of a non-ASCII letter (Unicode version dependent)")


def _lower(value):
    if isinstance(value, bytes):
        return value.lower()
    _case_safe(value)
    return value.lower()


def _upper(value):
    if isinstance(value, bytes):
        return value.upper()
    _case_safe(value)
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
    delimiters = node.args.get("expression")
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
    position = node.args.get("position")
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
    if node.args.get("replacement") is None:
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
    if node.args.get("start") is None:
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
    if node.args.get("occurrence") is not None and node.args.get("position") is None:
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
        raise Unsupported("ASCII of a non-ASCII first character (error or code, undocumented)")
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


def _chr(code: int) -> str:
    if code == 0:
        raise Unsupported("CHR(0) (documented as an empty string, observed as NUL)")
    if not _valid_code_point(code):
        raise EvalError(f"Invalid Unicode code point {code}")
    return chr(code)


@register("CHR")
def _chr_fn(c, node, args, cx):
    _arity(args, 1, 1, "CHR")
    a = _int_arg(c, args[0], "CHR")
    return E(T.STRING, _strict(_chr, a))


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
        raise EvalError("Duplicate character in source_characters of TRANSLATE")
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
