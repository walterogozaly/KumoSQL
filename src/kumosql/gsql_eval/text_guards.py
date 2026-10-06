"""Checks on the query text that the sqlglot tree cannot answer.

sqlglot reads some names BigQuery does not have as functions it knows (``LEN``, ``HEX``, ``CHARINDEX``...) and drops
arguments of a few functions it parses loosely, so the tree of a query BigQuery rejects can look valid. This scan
finds the function calls in the text itself (outside strings, quoted names and comments) and refuses the ones whose
meaning the tree cannot be trusted for. Refusing is always :class:`Unsupported`: the evaluator never claims BigQuery
rejects something it only suspects.
"""

from __future__ import annotations

import re

from .errors import Unsupported

# Names sqlglot maps onto functions of its own although BigQuery has no function of that name.
NOT_BIGQUERY = frozenset(
    "len lcase ucase startswith endswith charindex locate hex unhex regexp_like levenshtein char str_position "
    "isnull nvl nvl2 iif lcase to_date to_char to_varchar listagg group_concat substring_index "
    "date_format str_to_date unix_timestamp from_unixtime now sysdate getdate datediff dateadd".split()
)
# BigQuery's own spellings that sqlglot would not tell apart from the above are fine; these allow only the stated arity.
MAX_ARGS = {"length": 1, "char_length": 1, "character_length": 1, "byte_length": 1, "octet_length": 1, "lower": 1,
            "upper": 1, "reverse": 1, "split": 3, "lpad": 3, "rpad": 3, "edit_distance": 3, "ascii": 1, "chr": 1,
            "to_hex": 1, "from_hex": 1, "to_base64": 1, "from_base64": 1, "soundex": 1, "unicode": 1}
_WORD = re.compile(r"[A-Za-z_][A-Za-z_0-9]*")


def calls(sql: str) -> list[tuple[str, int, bool]]:
    """``(lowercase name, top-level argument count, dotted)`` for every ``name(`` in the text outside literals."""

    out: list[tuple[str, int, bool]] = []
    stack: list[list] = []  # [name or None, args seen, saw any token]
    i, size = 0, len(sql)
    prev_dot = False
    while i < size:
        c = sql[i]
        if sql.startswith("--", i) or c == "#":
            j = sql.find("\n", i)
            i = size if j < 0 else j
        elif sql.startswith("/*", i):
            j = sql.find("*/", i + 2)
            i = size if j < 0 else j + 2
        elif c == "`":
            j = i + 1
            while j < size and sql[j] != "`":
                j += 2 if sql[j] == "\\" else 1
            i = j + 1
            prev_dot = False
            if stack:
                stack[-1][2] = True
        elif c in "'\"":
            quote = c * 3 if sql.startswith(c * 3, i) else c
            raw = i > 0 and sql[i - 1] in "rR"
            j = i + len(quote)
            while j < size and not sql.startswith(quote, j):
                j += 2 if (sql[j] == "\\" and not raw) else 1
            i = j + len(quote)
            prev_dot = False
            if stack:
                stack[-1][2] = True
        elif c.isalpha() or c == "_":
            m = _WORD.match(sql, i)
            word = m.group(0)
            i = m.end()
            k = i
            while k < size and sql[k].isspace():
                k += 1
            if k < size and sql[k] == "(":
                stack.append([word.lower(), 1, False, prev_dot])
                i = k + 1
            elif stack:
                stack[-1][2] = True
            prev_dot = False
            continue
        elif c == "(":
            stack.append([None, 1, False, False])
            i += 1
            prev_dot = False
        elif c == ")":
            if stack:
                name, args, seen, dotted = (stack.pop() + [False])[:4]
                if name is not None:
                    out.append((name, args if seen else 0, bool(dotted)))
                if stack:
                    stack[-1][2] = True
            i += 1
            prev_dot = False
        elif c == "," and stack:
            stack[-1][1] += 1
            i += 1
        else:
            prev_dot = c == "."
            if not c.isspace() and stack:
                stack[-1][2] = True
            i += 1
    return out


def check(sql: str) -> None:
    for name, args, dotted in calls(sql):
        if dotted:
            continue
        if name in NOT_BIGQUERY:
            raise Unsupported(f"{name.upper()} is not a BigQuery function (sqlglot reads it as one it knows)")
        if name in MAX_ARGS and args > MAX_ARGS[name]:
            raise Unsupported(f"{name.upper()} with {args} arguments (sqlglot drops the extra ones)")
