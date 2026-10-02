"""Prove that a rewrite changed only layout, for any BigQuery statement.

A formatter changes whitespace, line breaks and the case of keywords and built-in function names. That
keeps the meaning of every statement, including ones sqlglot cannot parse (``LOAD DATA``, ``REPEAT``,
``CHANGES(TABLE t, ...)``) or keeps as an opaque command (``ALTER SCHEMA``, ``CREATE ROW ACCESS POLICY``),
so those can be proven by comparing tokens instead of query trees.

Two texts are layout equivalent when sqlglot's BigQuery tokenizer reads both completely (nothing but
whitespace and comments between tokens), they have the same token types in the same order, the same
comments in the same order, and every token is spelled the same except for the case of:

* a reserved keyword (``SELECT``, ``FROM``, ``PARTITION``) that is not part of a dotted path; reserved
  words can never be names, and
* a call to a built-in function (``count(x)``, ``date(ts)``) that is not dotted, does not follow a word
  that introduces a table or routine name, and is not a function the text itself creates. User-defined
  function and table names are case sensitive in BigQuery.

Unreserved keywords (``TEMP``, ``OPTIONS``, ``DATE``) can be table names, so their case must match, as
must string literals, quoted identifiers and numbers, character for character. This is the fallback
check for statements the query prover cannot read; it never replaces a proof it could make.
"""

from __future__ import annotations

import re
from functools import lru_cache

from sqlglot.dialects.bigquery import BigQuery
from sqlglot.tokens import Token, TokenType

_GAP_RE = re.compile(r"(?:\s|--[^\n]*|#[^\n]*|/\*.*?\*/)*", re.S)
_CREATED_FUNCTION_RE = re.compile(
    r"\bCREATE\s+(?:OR\s+REPLACE\s+)?(?:TEMP(?:ORARY)?\s+)?(?:AGGREGATE\s+|TABLE\s+)?FUNCTION\s+"
    r"(?:IF\s+NOT\s+EXISTS\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*\(",
    re.I,
)
# GoogleSQL's reserved keywords (those sqlfluff's BigQuery dialect also reserves).
_RESERVED = frozenset(
    """ALL AND ANY ARRAY AS ASC ASSERT_ROWS_MODIFIED AT BETWEEN BY CASE CAST COLLATE CONTAINS CREATE CROSS
    CUBE CURRENT DEFAULT DEFINE DESC DISTINCT ELSE END ENUM ESCAPE EXCEPT EXCLUDE EXISTS FALSE FETCH FOLLOWING
    FOR FROM FULL GROUP GROUPING GROUPS HASH HAVING IF IGNORE IN INNER INTERSECT INTERVAL INTO IS JOIN LATERAL
    LEFT LIKE LIMIT LOOKUP MERGE NEW NO NOT NULL NULLS OF ON OR ORDER OUTER OVER PARTITION PRECEDING PROTO
    RANGE RECURSIVE RESPECT RIGHT ROLLUP ROWS SELECT SET SOME STRUCT TABLESAMPLE THEN TO TREAT TRUE UNBOUNDED
    UNION UNNEST USING WHEN WHERE WINDOW WITH WITHIN""".split()
)
# Words after which a name is a table, view, routine or other case-sensitive object.
_NAME_INTRODUCERS = frozenset(
    """FROM JOIN INTO TABLE VIEW FUNCTION PROCEDURE MODEL SCHEMA DATASET UPDATE MERGE USING EXISTS CALL
    SNAPSHOT INDEX POLICY ON""".split()
)
_VERBATIM = (TokenType.IDENTIFIER, TokenType.NUMBER)
# Tokens whose meaning depends on touching their neighbour (``@@error``, ``@param``, ``a.b``).
_ADJACENT = (TokenType.PARAMETER, TokenType.DOT)


class _Tokenizer(BigQuery.Tokenizer):
    """BigQuery's tokenizer without command mode, which swallows the rest of ``CALL``, ``REPEAT`` and
    other statements sqlglot cannot parse as one string, whitespace included."""

    COMMANDS: set = set()


@lru_cache(maxsize=1)
def _builtin_functions() -> frozenset[str]:
    parser = BigQuery.Parser
    return frozenset(
        name.upper() for name in (*parser.FUNCTIONS, *parser.FUNCTION_PARSERS, *parser.NO_PAREN_FUNCTIONS)
        if isinstance(name, str)
    )


def tokenize_exactly(sql: str) -> list[Token] | None:
    """Every token of ``sql``, or None when the tokenizer fails or skips anything but whitespace and comments."""

    try:
        tokens = _Tokenizer(dialect=BigQuery()).tokenize(sql)
    except Exception:  # noqa: BLE001
        return None
    position = 0
    for token in tokens:
        if token.start < position or not _GAP_RE.fullmatch(sql, position, token.start):
            return None
        position = token.end + 1
    if not _GAP_RE.fullmatch(sql, position, len(sql)):
        return None
    return tokens


def _verbatim(token: Token) -> bool:
    return token.token_type in _VERBATIM or token.token_type.name.endswith("STRING")


def _spelling(sql: str, token: Token) -> str:
    """The token as written; a multi-word keyword such as ``ORDER BY`` with its inner whitespace collapsed."""

    text = sql[token.start : token.end + 1]
    return text if _verbatim(token) else " ".join(text.split())


def _dotted(tokens: list[Token], index: int) -> bool:
    before = index > 0 and tokens[index - 1].token_type == TokenType.DOT
    after = index + 1 < len(tokens) and tokens[index + 1].token_type == TokenType.DOT
    return before or after


def _builtin_call(tokens: list[Token], index: int, created: set[str]) -> bool:
    name = tokens[index].text.upper()
    is_call = index + 1 < len(tokens) and tokens[index + 1].token_type == TokenType.L_PAREN
    after_name_word = index > 0 and tokens[index - 1].text.upper() in _NAME_INTRODUCERS
    return (
        is_call and not after_name_word and not _dotted(tokens, index)
        and name in _builtin_functions() and name not in created
    )


def _case_insensitive(tokens: list[Token], index: int, created: set[str]) -> bool:
    """Whether BigQuery reads this token the same whatever its case."""

    token = tokens[index]
    if _verbatim(token) or _dotted(tokens, index):
        return False
    words = token.text.upper().split()
    if token.token_type != TokenType.VAR and all(word in _RESERVED for word in words):
        return True
    return _builtin_call(tokens, index, created)


def _created_functions(*texts: str) -> set[str]:
    return {name.upper() for text in texts for name in _CREATED_FUNCTION_RE.findall(text)}


def _touching(tokens: list[Token], index: int) -> tuple[bool, bool]:
    """Whether the token touches the token before it and the token after it."""

    token = tokens[index]
    before = index > 0 and tokens[index - 1].end + 1 == token.start
    after = index + 1 < len(tokens) and token.end + 1 == tokens[index + 1].start
    return before, after


def _comments(tokens: list[Token]) -> list[str]:
    return [comment.strip() for token in tokens for comment in token.comments]


def _aligned(before: str, after: str):
    """Token lists of two texts with the same token types in the same order, or None."""

    left, right = tokenize_exactly(before), tokenize_exactly(after)
    if left is None or right is None or len(left) != len(right):
        return None
    if any(a.token_type != b.token_type for a, b in zip(left, right)):
        return None
    return left, right


def layout_only_change(before: str, after: str) -> bool:
    """True when ``after`` differs from ``before`` only in layout and in case where case does not matter."""

    aligned = _aligned(before, after)
    if aligned is None:
        return False
    left, right = aligned
    if _comments(left) != _comments(right):
        return False
    created = _created_functions(before, after)
    for index, (a, b) in enumerate(zip(left, right)):
        if a.token_type in _ADJACENT and _touching(left, index) != _touching(right, index):
            return False
        old, new = _spelling(before, a), _spelling(after, b)
        if old == new:
            continue
        if old.lower() != new.lower() or not _case_insensitive(left, index, created):
            return False
    return True


def restore_function_case(original: str, formatted: str) -> str:
    """Put back the original case of every call to a function that is not built in.

    sqlfluff upper-cases the name of any unqualified function call, including a temporary or
    user-defined function (``f(x)`` becomes ``F(x)``), whose name is case sensitive in BigQuery. When the
    token streams do not line up, ``formatted`` is returned unchanged and verification decides.
    """

    aligned = _aligned(original, formatted)
    if aligned is None:
        return formatted
    left, right = aligned
    created = _created_functions(original)
    pieces: list[str] = []
    position = 0
    for index, (a, b) in enumerate(zip(left, right)):
        old, new = _spelling(original, a), _spelling(formatted, b)
        if old == new or old.lower() != new.lower() or a.token_type != TokenType.VAR:
            continue
        is_call = index + 1 < len(left) and left[index + 1].token_type == TokenType.L_PAREN
        if is_call and not _builtin_call(left, index, created):
            pieces += [formatted[position : b.start], old]
            position = b.end + 1
    if not pieces:
        return formatted
    return "".join(pieces) + formatted[position:]
