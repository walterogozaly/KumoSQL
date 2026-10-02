"""One spelling per BigQuery string value, so the provers and the execution checks agree with BigQuery.

sqlglot keeps some backslash escapes of a BigQuery literal undecoded: ``'a\\"b'`` reads as the text ``a\\"b``,
which differs from ``'a"b'`` although BigQuery gives both the same string (and sqlglot writes the first back as
``'a\\\\"b'``, a different string). It also reads two adjacent quoted names, ``col``col``, as one name containing a
backtick. ``canonical_literals`` rewrites the source text before it is parsed: every plain string whose escapes
are all among ``\\\\ \\' \\" \\` \\? \\n \\r \\t`` becomes a single-quoted literal using only ``\\\\``, ``\\'``, ``\\n``,
``\\r`` and ``\\t``, and a space goes between adjacent backtick names. Raw (``r'..'``) and bytes (``b'..'``)
literals, strings with other escapes (``\\x41``, ``\\u0041``, octal) and everything else are left as written, so
equal values written with those escapes stay unproven rather than being called different by a wrong reading.
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


def _string_end(sql: str, start: int) -> tuple[int, str, str]:
    """``(end, quote, body)`` of the string literal whose opening quote is at ``start``; ``body`` is None if unterminated."""

    quote = sql[start] * 3 if sql.startswith(sql[start] * 3, start) else sql[start]
    j = start + len(quote)
    while j < len(sql) and not sql.startswith(quote, j):
        j += 2 if sql[j] == "\\" else 1
    if not sql.startswith(quote, j):
        return len(sql), quote, None
    return j + len(quote), quote, sql[start + len(quote) : j]


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
                end = _string_end(sql, j)[0]  # a raw or bytes literal: copied as written
                out.append(sql[i:end])
                i = end
            else:
                out.append(word)
                i = j
        elif char in "'\"":
            end, _, body = _string_end(sql, i)
            value = None if body is None else _decode(body)
            out.append(sql[i:end] if value is None else "'" + "".join(_OUT.get(c, c) for c in value) + "'")
            i = end
        else:
            out.append(char)
            i += 1
    return "".join(out)
