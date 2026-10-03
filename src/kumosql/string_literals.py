"""One spelling per BigQuery string value, so the provers and the execution checks agree with BigQuery.

sqlglot keeps some backslash escapes of a BigQuery literal undecoded: ``'a\\"b'`` reads as the text ``a\\"b``,
which differs from ``'a"b'`` although BigQuery gives both the same string (and sqlglot writes the first back as
``'a\\\\"b'``, a different string). It also reads two adjacent quoted names, ``col``col``, as one name containing a
backtick. ``canonical_literals`` rewrites the source text before it is parsed: every plain string whose escapes
are all among ``\\\\ \\' \\" \\` \\? \\n \\r \\t`` becomes a single-quoted literal using only ``\\\\``, ``\\'``, ``\\n``,
``\\r`` and ``\\t``, and a space goes between adjacent backtick names. Raw strings (``r'..'``), strings with other
escapes (``\\x41``, ``\\u0041``, octal) and everything else are left as written, so equal values written with those
escapes stay unproven rather than being called different by a wrong reading.

Bytes literals (``b'..'``, and raw ``rb'..'``) are decoded and written back with printable ASCII as is and every other
byte as ``\\xHH``. sqlglot reads ``\\\\`` in a bytes literal as one backslash but keeps ``\\x41`` undecoded, so
``b'\\\\x41'`` (four bytes) and ``b'\\x41'`` (the one byte ``A``) used to read the same.

A single-quoted literal holding a line break is not valid GoogleSQL and is left as written.
"""

from __future__ import annotations

_SIMPLE = {"\\": "\\", "'": "'", '"': '"', "`": "`", "?": "?", "n": "\n", "r": "\r", "t": "\t"}
_OUT = {"\\": "\\\\", "'": "\\'", "\n": "\\n", "\r": "\\r", "\t": "\\t"}


def _decode(body: str) -> str | None:
    """The string value of an escaped literal body, ``None`` if it uses an escape this module does not decode."""

    out = []
    i = 0
    while i < len(body):
        char = body[i]
        if char != "\\":
            out.append(char)
            i += 1
            continue
        if i + 1 >= len(body) or body[i + 1] not in _SIMPLE:
            return None
        out.append(_SIMPLE[body[i + 1]])
        i += 2
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

    if "\n" not in sql and "\r" not in sql:
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
            i = end
        else:
            i += 1
    return False


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
