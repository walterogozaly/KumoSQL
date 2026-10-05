"""One spelling per BigQuery string value, so the provers and the execution checks agree with BigQuery.

sqlglot keeps some backslash escapes of a BigQuery literal undecoded: ``'a\\"b'`` reads as the text ``a\\"b``,
which differs from ``'a"b'`` although BigQuery gives both the same string (and sqlglot writes the first back as
``'a\\\\"b'``, a different string). It also reads two adjacent quoted names, ``col``col``, as one name containing a
backtick. ``canonical_literals`` rewrites the source text before it is parsed: every plain string has its escapes
decoded (``\\\\ \\' \\" \\` \\? \\a \\b \\f \\n \\r \\t \\v``, ``\\x41`` and octal below 128, ``\\u0041``, ``\\U00000041``) and becomes a
single-quoted literal using only ``\\\\``, ``\\'``, ``\\n``, ``\\r``, ``\\t`` and the control escapes, and a space goes between adjacent
backtick names. Raw strings (``r'..'``) and everything else are left as written. A string with an escape whose value is
not certain (``\\xE9``, which BigQuery may read as a byte) is reported by ``invalid_literal``, so the provers decline it:
sqlglot reads ``'\\x41'`` and ``'\\\\x41'`` as the same four characters, and the structural prover once proved them equal.

Bytes literals (``b'..'``, and raw ``rb'..'``) are decoded and written back with printable ASCII as is and every other
byte as ``\\xHH``. sqlglot reads ``\\\\`` in a bytes literal as one backslash but keeps ``\\x41`` undecoded, so
``b'\\\\x41'`` (four bytes) and ``b'\\x41'`` (the one byte ``A``) used to read the same.

A single-quoted literal holding a line break is not valid GoogleSQL and is left as written (and declined by ``invalid_literal``).
"""

from __future__ import annotations

_SIMPLE = {"\\": "\\", "'": "'", '"': '"', "`": "`", "?": "?", "n": "\n", "r": "\r", "t": "\t", "a": "\a", "b": "\b", "f": "\f", "v": "\v"}
_OUT = {"\\": "\\\\", "'": "\\'", "\n": "\\n", "\r": "\\r", "\t": "\\t", "\a": "\\a", "\b": "\\b", "\f": "\\f", "\v": "\\v"}
_HEX_DIGITS = "0123456789abcdefABCDEF"


def _decode(body: str) -> str | None:
    """The string value of an escaped literal body, ``None`` if it uses an escape whose value this module cannot be sure of.

    ``\\xhh`` and an octal ``\\ooo`` are decoded only below 128, where a character and a byte are the same thing (above that
    BigQuery may read them as UTF-8 bytes); ``\\uhhhh`` and ``\\Uhhhhhhhh`` are code points. sqlglot decodes some of these and
    keeps the others as text, so ``'\\x41'`` and ``'\\\\x41'`` both reach it as the four characters ``\\x41``.
    """

    out = []
    i = 0
    while i < len(body):
        char = body[i]
        if char != "\\":
            out.append(char)
            i += 1
            continue
        code = body[i + 1 : i + 2]
        if code in _SIMPLE:
            out.append(_SIMPLE[code])
            i += 2
            continue
        if code in ("x", "X"):
            width, base, skip = 2, 16, 2
        elif code in ("u", "U"):
            width, base, skip = (4 if code == "u" else 8), 16, 2
        elif code in "01234567" and code:
            width, base, skip = 3, 8, 1
        else:
            return None
        digits = body[i + skip : i + skip + width]
        allowed = _HEX_DIGITS if base == 16 else "01234567"
        if len(digits) != width or any(d not in allowed for d in digits):
            return None
        value = int(digits, base)
        if code not in ("u", "U") and value >= 128:
            return None
        if value == 0 or value > 0x10FFFF or 0xD800 <= value <= 0xDFFF:
            return None
        out.append(chr(value))
        i += skip + width
    return "".join(out)


_BYTE_ESCAPES = {"a": 7, "b": 8, "f": 12, "n": 10, "r": 13, "t": 9, "v": 11, "\\": 92, "?": 63, '"': 34, "'": 39, "`": 96}
_HEX = "0123456789abcdefABCDEF"


def _decode_bytes(body: str, *, raw: bool) -> bytes | None:
    """The value of a bytes literal body (UTF-8 text plus BigQuery's escapes), ``None`` for an escape it does not have."""

    if raw:
        return body.encode("utf-8")
    out = bytearray()
    i = 0
    while i < len(body):
        char = body[i]
        if char != "\\":
            out += char.encode("utf-8")
            i += 1
            continue
        code = body[i + 1 : i + 2]
        digits = body[i + 2 : i + 4]
        if code in _BYTE_ESCAPES:
            out.append(_BYTE_ESCAPES[code])
            i += 2
        elif code in ("x", "X") and len(digits) == 2 and all(d in _HEX for d in digits):
            out.append(int(digits, 16))
            i += 4
        elif len(octal := body[i + 1 : i + 4]) == 3 and all(d in "01234567" for d in octal) and int(octal, 8) < 256:
            out.append(int(octal, 8))
            i += 4
        else:
            return None
    return bytes(out)


def _bytes_literal(value: bytes) -> str:
    """``value`` as ``b'..'`` with printable ASCII kept and every other byte, quote and backslash written ``\\xHH``.

    With no ``\\\\`` left, sqlglot has exactly one reading of each byte string.
    """

    return "b'" + "".join(chr(b) if 32 <= b < 127 and b not in (39, 92) else f"\\x{b:02X}" for b in value) + "'"


def _string_end(sql: str, start: int) -> tuple[int, str, str]:
    """``(end, quote, body)`` of the string literal whose opening quote is at ``start``; ``body`` is None if unterminated."""

    quote = sql[start] * 3 if sql.startswith(sql[start] * 3, start) else sql[start]
    j = start + len(quote)
    while j < len(sql) and not sql.startswith(quote, j):
        j += 2 if sql[j] == "\\" else 1
    if not sql.startswith(quote, j):
        return len(sql), quote, None
    return j + len(quote), quote, sql[start + len(quote) : j]


def _invalid(quote: str, body: str | None) -> bool:
    """Whether the literal is unterminated, or holds a line break GoogleSQL only allows inside triple quotes.

    Such a literal is copied as written: rewriting ``b'a<newline>b'`` as ``b'a\\x0Ab'`` would turn a query
    BigQuery rejects into one it runs.
    """

    return body is None or (len(quote) == 1 and ("\n" in body or "\r" in body))


def invalid_literal(sql: str) -> bool:
    """Whether ``sql`` has a single-quoted string or bytes literal holding a line break.

    GoogleSQL rejects such a query, while sqlglot reads the literal, so the provers decline it rather than prove
    it equal to a valid query with the escaped spelling.
    """

    if "\n" not in sql and "\r" not in sql and "\\" not in sql:
        return False
    i, size = 0, len(sql)
    while i < size:
        char = sql[i]
        if sql.startswith("--", i) or char == "#":
            end = sql.find("\n", i)
            i = size if end < 0 else end
        elif sql.startswith("/*", i):
            end = sql.find("*/", i + 2)
            i = size if end < 0 else end + 2
        elif char == "`":
            j = i + 1
            while j < size and sql[j] != "`":
                j += 2 if sql[j] == "\\" else 1
            i = j + 1
        elif char in "'\"":
            end, quote, body = _string_end(sql, i)
            if body is not None and _invalid(quote, body):
                return True
            if body is not None and "\\" in body and not _prefixed(sql, i) and _decode(body) is None:
                return True  # an escape whose value is not certain: sqlglot would read it the same as a backslash and text
            i = end
        else:
            i += 1
    return False


def _prefixed(sql: str, start: int) -> bool:
    """Whether the quote at ``start`` opens a raw or bytes literal (``r'..'``, ``b'..'``, ``rb'..'``): a prefix not part of a longer word."""

    j = start
    while j > 0 and sql[j - 1] in "rRbB":
        j -= 1
    return start - j in (1, 2) and not (j > 0 and (sql[j - 1].isalnum() or sql[j - 1] == "_"))


def canonical_literals(sql: str) -> str:
    """``sql`` with BigQuery string literals and adjacent quoted names spelled one way (see the module docstring)."""

    if "\\" not in sql and "``" not in sql:
        return sql
    out = []
    i, size = 0, len(sql)
    while i < size:
        char = sql[i]
        if sql.startswith("--", i) or char == "#":
            end = sql.find("\n", i)
            end = size if end < 0 else end
            out.append(sql[i:end])
            i = end
        elif sql.startswith("/*", i):
            end = sql.find("*/", i + 2)
            end = size if end < 0 else end + 2
            out.append(sql[i:end])
            i = end
        elif char == "`":
            j = i + 1
            while j < size and sql[j] != "`":
                j += 2 if sql[j] == "\\" else 1
            j = min(j + 1, size)
            out.append(sql[i:j])
            if sql.startswith("`", j):
                out.append(" ")  # `a``b` is two names, not one name containing a backtick
            i = j
        elif char.isalnum() or char == "_":
            j = i
            while j < size and (sql[j].isalnum() or sql[j] == "_"):
                j += 1
            word = sql[i:j]
            if word.lower() in ("r", "b", "rb", "br") and j < size and sql[j] in "'\"":
                end, quote, body = _string_end(sql, j)
                value = None if _invalid(quote, body) or word.lower() == "r" else _decode_bytes(body, raw=len(word) == 2)
                out.append(sql[i:end] if value is None else _bytes_literal(value))  # a raw string: copied as written
                i = end
            else:
                out.append(word)
                i = j
        elif char in "'\"":
            end, quote, body = _string_end(sql, i)
            value = None if _invalid(quote, body) else _decode(body)
            out.append(sql[i:end] if value is None else "'" + "".join(_OUT.get(c, c) for c in value) + "'")
            i = end
        else:
            out.append(char)
            i += 1
    return "".join(out)
