"""Dataform SQLX handling shared by every rewrite rule.

SQLX files are split into ``config``/``js``/``pre_operations``/
``post_operations`` blocks (and the ``input "name"`` blocks of a Dataform
test), which are preserved byte-for-byte, and SQL sections. ``${...}`` interpolations inside SQL sections are masked with
SQL-safe sentinels during parsing and restored afterward.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import re


_SQLX_BLOCK_RE = re.compile(
    r"""(?im)^[ \t]*(?:(?:config|js|pre_operations|post_operations)|input\s+(?:"[^"\n]*"|'[^'\n]*'))\s*\{"""
)
_SQLX_WHEN_CONNECTIVE_RE = re.compile(r"\s*when\s*\([^,()]*(?:\([^()]*\))?[^,()]*,\s*[`'\"]\s*(AND|OR)\b", re.IGNORECASE)
_SQLX_CLAUSE_RE = re.compile(r"\b(WHERE|QUALIFY|HAVING|ORDER\s+BY)\b", re.IGNORECASE)
_TOKEN_RE = re.compile(r"__sqlx_token_\d+__")
_QUOTED = r"""(?:"[^"\\]*"|'[^'\\]*'|`[^`\\]*`)"""
_TABLE_REFERENCE_RE = re.compile(
    rf"^\$\{{\s*(?:(?:ref|resolve)\s*\(\s*{_QUOTED}(?:\s*,\s*{_QUOTED})?\s*\)|self\s*\(\s*\))\s*\}}$"
)


class SqlxRestorationError(ValueError):
    """Raised when a rewrite loses or duplicates a masked SQLX interpolation."""


def looks_like_sqlx(sql: str) -> bool:
    return bool(_SQLX_BLOCK_RE.search(sql) or "${" in sql)


def _find_balanced_brace(text: str, opening: int) -> int:
    """Return the closing brace for a SQLX block, or raise on malformed input."""

    depth = 1
    index = opening + 1
    quote: str | None = None
    line_comment = False
    block_comment = False
    while index < len(text):
        char = text[index]
        next_char = text[index + 1] if index + 1 < len(text) else ""
        if line_comment:
            if char in "\r\n":
                line_comment = False
        elif block_comment:
            if char == "*" and next_char == "/":
                block_comment = False
                index += 1
        elif quote:
            if char == "\\":
                index += 1
            elif char == quote:
                if next_char == quote:
                    index += 1
                else:
                    quote = None
        elif char == "-" and next_char == "-":
            line_comment = True
            index += 1
        elif char == "/" and next_char == "*":
            block_comment = True
            index += 1
        elif char == "/" and next_char == "/":
            line_comment = True  # a JavaScript comment: its apostrophes and braces are not code
            index += 1
        elif char in "'\"`":
            quote = char
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return index
        index += 1
    raise ValueError("unterminated SQLX block")


def split_sqlx_sections(sql: str) -> list[tuple[str, str]]:
    """Split SQLX into ``("sql", text)`` and ``("block", text)`` pieces."""

    pieces: list[tuple[str, str]] = []
    cursor = 0
    while match := _SQLX_BLOCK_RE.search(sql, cursor):
        if match.start() > cursor:
            pieces.append(("sql", sql[cursor : match.start()]))
        opening = sql.find("{", match.start(), match.end())
        closing = _find_balanced_brace(sql, opening)
        pieces.append(("block", sql[match.start() : closing + 1]))
        cursor = closing + 1
    pieces.append(("sql", sql[cursor:]))
    return pieces


def _find_interpolation_end(text: str, opening: int) -> int:
    depth = 1
    index = opening + 2
    quote: str | None = None
    while index < len(text):
        char = text[index]
        next_char = text[index + 1] if index + 1 < len(text) else ""
        if quote:
            if char == "\\":
                index += 1
            elif char == quote:
                if next_char == quote:
                    index += 1
                else:
                    quote = None
        elif char in "'\"`":
            quote = char
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return index
        index += 1
    raise ValueError("unterminated SQLX interpolation")


def sql_comment_spans(sql: str) -> list[tuple[int, int]]:
    """``(start, end)`` of every ``--`` and ``/* */`` comment in ``sql``.

    Dataform leaves a ``${...}`` inside these as literal text: it is neither evaluated nor a dependency. A ``#`` comment
    is not one of them (checked against ``@dataform/cli`` 3.0.71, which evaluates ``${ref(...)}`` after ``#``), nor is a
    ``--`` inside a string.
    Quotes inside a comment do not open a string, and comment markers inside a string or a ``${...}``
    expression do not open a comment. An unterminated block comment runs to the end of the text.
    """

    spans: list[tuple[int, int]] = []
    index, size = 0, len(sql)
    while index < size:
        char = sql[index]
        if sql.startswith("${", index):
            try:
                index = _find_interpolation_end(sql, index) + 1
            except ValueError:
                return spans
        elif sql.startswith("--", index):
            end = index
            while end < size and sql[end] not in "\r\n":
                end += 1
            spans.append((index, end))
            index = end
        elif sql.startswith("/*", index):
            end = sql.find("*/", index + 2)
            end = size if end < 0 else end + 2
            spans.append((index, end))
            index = end
        elif char in "'\"`":
            quote = sql[index : index + 3] if sql[index : index + 3] in ("'''", '"""') else char
            index += len(quote)
            while index < size and not sql.startswith(quote, index):
                if sql.startswith("${", index):
                    try:
                        index = _find_interpolation_end(sql, index) + 1
                    except ValueError:
                        return spans
                    continue
                index += 2 if sql[index] == "\\" else 1
            index += len(quote)
        else:
            index += 1
    return spans


def outside_sql_comments(sql: str, pattern: re.Pattern[str]) -> list[re.Match[str]]:
    """The matches of ``pattern`` in ``sql`` that do not start inside a SQL comment."""

    spans = sql_comment_spans(sql)
    return [m for m in pattern.finditer(sql) if not any(start <= m.start() < end for start, end in spans)]


@dataclass(frozen=True)
class SqlxRestoration:
    token: str
    original: str
    pattern: re.Pattern[str]


def mask_sqlx_interpolations(sql: str) -> tuple[str, tuple[SqlxRestoration, ...]]:
    """Replace SQLX expressions with SQL-safe sentinels and remember how to restore them."""

    output: list[str] = []
    restorations: list[SqlxRestoration] = []
    cursor = 0
    ordinal = 0
    while True:
        opening = sql.find("${", cursor)
        if opening < 0:
            output.append(sql[cursor:])
            break

        output.append(sql[cursor:opening])
        closing = _find_interpolation_end(sql, opening)
        original = sql[opening : closing + 1]
        token = f"__sqlx_token_{ordinal:03d}__"
        while token in sql:
            ordinal += 1
            token = f"__sqlx_token_{ordinal:03d}__"
        ordinal += 1

        prefix = "".join(output).rstrip()
        body = original[2:-1]
        clause_match = _SQLX_CLAUSE_RE.search(body)
        preceding_clause = re.search(
            r"\b(?:WHERE|QUALIFY|HAVING|ORDER\s+BY)\s*$",
            prefix,
            re.IGNORECASE,
        )
        preceding_condition = re.search(r"\b(?:AND|OR)\s*$", prefix, re.IGNORECASE)

        replacement = token
        pattern = re.compile(re.escape(token))
        connective = _SQLX_WHEN_CONNECTIVE_RE.match(body)
        if connective and not clause_match and not preceding_clause and not preceding_condition:
            # ``... WHERE a > 0 ${when(incremental(), `AND b > 1`)}``: the expression continues the condition.
            keyword = connective.group(1).upper()
            replacement = f"{keyword} {token}"
            pattern = re.compile(rf"\b{keyword}\s+{re.escape(token)}\b", re.IGNORECASE)
        elif clause_match and not preceding_clause and not preceding_condition:
            keyword = clause_match.group(1).upper()
            if keyword in {"WHERE", "QUALIFY", "HAVING"}:
                replacement = f"{keyword} {token}"
                pattern = re.compile(rf"\b{keyword}\s+{re.escape(token)}\b", re.IGNORECASE)
            elif keyword == "ORDER BY":
                replacement = f"ORDER BY {token}"
                pattern = re.compile(rf"\bORDER\s+BY\s+{re.escape(token)}\b", re.IGNORECASE)

        output.append(replacement)
        restorations.append(SqlxRestoration(token, original, pattern))
        cursor = closing + 1

    return "".join(output), tuple(restorations)


def is_table_reference(original: str) -> bool:
    """``${ref("t")}``, ``${ref("dataset", "t")}``, ``${resolve("t")}`` or ``${self()}``: one table name once compiled."""

    return bool(_TABLE_REFERENCE_RE.match(original))


def opaque_tokens(restorations: tuple[SqlxRestoration, ...]) -> frozenset[str]:
    """Sentinels of the interpolations that are not table references.

    Such an expression (``${"x OR y"}``, ``${when(incremental(), "AND b > 1")}``) compiles to arbitrary SQL text:
    a clause, a predicate that binds looser than its neighbours, or a query that reads a CTE. A rule sees only an
    opaque name in its place.
    """

    return frozenset(item.token for item in restorations if not is_table_reference(item.original))


def restore_sqlx_interpolations(sql: str, restorations: tuple[SqlxRestoration, ...]) -> str:
    # A successful SQL parse does not prove that sqlglot retained every opaque
    # SQLX expression. Check the sentinels before restoring any of them so a
    # lost or duplicated incremental filter cannot silently become valid SQL.
    for item in restorations:
        count = sql.count(item.token)
        if count != 1:
            raise SqlxRestorationError(
                "SQLX interpolation placeholder was lost or duplicated during rewriting "
                f"({count} copies found; expected one)"
            )

    restored = sql
    for item in restorations:
        # A function replacement is inserted as is; a string one would read
        # the backslashes in ``r'\d'`` or ``\1`` as replacement syntax.
        restored = item.pattern.sub(lambda _match, original=item.original: original, restored)
        # sqlglot can quote an identifier sentinel when it occurs inside a
        # quoted table reference. Restore that spelling too.
        restored = restored.replace(f"`{item.token}`", item.original)
        restored = restored.replace(item.token, item.original)
    return restored


def mask_sqlx_by_content(sql: str) -> str:
    """Mask interpolations with sentinels derived from their text, not position.

    A rewrite may move an interpolation (for example, inlining a CTE moves its
    ``${ref(...)}``), which changes positional sentinel numbers. Content-derived
    sentinels let the equivalence checker treat identical interpolation text as
    the same opaque fragment on both sides.
    """

    masked, restorations = mask_sqlx_interpolations(sql)
    by_token = {item.token: item.original for item in restorations}

    def replace(match: re.Match[str]) -> str:
        original = by_token.get(match.group(0))
        if original is None:
            return match.group(0)
        digest = hashlib.sha256(original.encode("utf-8")).hexdigest()[:16]
        return f"__sqlx_{digest}__"

    return _TOKEN_RE.sub(replace, masked)


def with_preserved_whitespace(original: str, transformed: str) -> str:
    if not transformed:
        return original
    leading = re.match(r"\s*", original).group(0)
    trailing = re.search(r"\s*$", original).group(0)
    return f"{leading}{transformed.strip()}{trailing}"
