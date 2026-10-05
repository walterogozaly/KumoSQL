"""Refuse a proof whose input names a type BigQuery does not have.

sqlglot reads many other engines' type names and prints them as BigQuery types: ``FLOAT``, ``REAL`` and
``DOUBLE`` become ``FLOAT64``, ``INT32`` and ``INT8`` become ``INT64``, ``VARCHAR``, ``TEXT``, ``CHAR`` and
``UUID`` become ``STRING``, ``BINARY`` becomes ``BYTES``. BigQuery rejects every one of them ("Type not found"),
so a proof of ``CAST(x AS FLOAT)`` against ``CAST(x AS FLOAT64)`` credits a pair that has nothing to preserve,
and the parsed trees can no longer tell the two apart. The names are only visible in the source text, so this
module reads the tokens and checks each type keyword in the places where a query names a type against the
names BigQuery accepts. Every name in the list was accepted by a BigQuery dry run (2026-10-04; the others,
including those above, returned "Type not found").

The places read are the ones where a type keyword cannot be a column name:

- the target type of ``CAST``, ``SAFE_CAST`` and ``TRY_CAST``, and of ``expr::type``;
- any ``ARRAY<...>`` or ``STRUCT<...>`` (a typed literal, a column or a parameter), including types nested in
  them, and ``RANGE<...>`` inside a cast; a struct field named like a type (``STRUCT<text STRING>``) is a name,
  not a type;
- a typed literal such as ``FLOAT '1'`` (a type keyword directly followed by a string);
- the column and parameter list of ``CREATE TABLE``, ``CREATE FUNCTION`` and ``CREATE PROCEDURE``, a
  ``RETURNS`` type, and the type of a ``DECLARE``.

It is a refusal list, not a validator. A word the tokenizer does not treat as a type (a user-defined name) is left
to the parser, which already refuses it. A bare type keyword anywhere else (``ALTER TABLE ... ADD COLUMN a
FLOAT``, a type inside a ``BEGIN`` block's later statements that the tokenizer reads as one command) is not read,
because a column or alias may be spelled the same (``SELECT a float FROM t`` is valid), so reading it would refuse
valid queries.
"""

from __future__ import annotations

from sqlglot import TokenType, tokenize
from sqlglot.parser import Parser

BIGQUERY_TYPE_NAMES = frozenset({
    "INT64", "INT", "SMALLINT", "INTEGER", "BIGINT", "TINYINT", "BYTEINT",
    "NUMERIC", "DECIMAL", "BIGNUMERIC", "BIGDECIMAL", "FLOAT64",
    "BOOL", "BOOLEAN", "STRING", "BYTES",
    "DATE", "DATETIME", "TIME", "TIMESTAMP", "INTERVAL", "GEOGRAPHY", "JSON",
    "ARRAY", "STRUCT", "RANGE",
})

_CASTS = {"CAST", "SAFE_CAST", "TRY_CAST"}
_ANGLE_TYPES = {TokenType.ARRAY, TokenType.STRUCT}
# What can sit between the angle brackets of a type: type words, field names, commas, nested brackets and the
# length or precision of a type. Anything else means the text is not a type after all.
_IN_ANGLES = {TokenType.VAR, TokenType.IDENTIFIER, TokenType.COMMA, TokenType.LT, TokenType.GT, TokenType.L_PAREN,
              TokenType.R_PAREN, TokenType.NUMBER}
_NOT_COLUMNS = {"CONSTRAINT", "PRIMARY", "PRIMARY KEY", "FOREIGN", "FOREIGN KEY", "UNIQUE", "CHECK"}
_NO_COLUMN_LIST = {"AS", "LIKE", "CLONE", "COPY", "SELECT", "WITH", "OPTIONS", "PARTITION", "CLUSTER", "RETURNS", "LANGUAGE"}


def _reason(item) -> str:
    return f"the type name {item.text.upper()} does not exist in BigQuery (it reports \"Type not found\")"


def _is_type(item, types) -> bool:
    return item.token_type in types


def _bad_type(tokens: list, types) -> str | None:
    """The first type word in ``tokens`` (the tokens of one written type) that BigQuery does not have."""

    angles = 0
    for index, item in enumerate(tokens):
        if item.token_type is TokenType.LT:
            angles += 1
        elif item.token_type is TokenType.GT:
            angles -= 1
        elif _is_type(item, types) and item.text.upper() not in BIGQUERY_TYPE_NAMES:
            following = tokens[index + 1] if index + 1 < len(tokens) else None
            if angles > 0 and following is not None and _is_type(following, types):
                continue  # a struct field named like a type: STRUCT<text STRING>
            return _reason(item)
    return None


def _casts(tokens: list, types) -> str | None:
    for start, token in enumerate(tokens):
        if token.text.upper() not in _CASTS or start + 1 >= len(tokens) or tokens[start + 1].token_type is not TokenType.L_PAREN:
            continue
        depth = 0
        target: list = []
        in_type = False
        for item in tokens[start + 1:]:
            if item.token_type is TokenType.L_PAREN:
                depth += 1
            elif item.token_type is TokenType.R_PAREN:
                depth -= 1
                if depth == 0:
                    break
            elif depth == 1 and item.token_type is TokenType.ALIAS:
                in_type, target = True, []
                continue
            elif in_type and item.token_type is TokenType.FORMAT:
                break
            if in_type:
                target.append(item)
        found = _bad_type(target, types)
        if found:
            return found
    return None


def _angle_types(tokens: list, types) -> str | None:
    for start, token in enumerate(tokens):
        if token.token_type not in _ANGLE_TYPES or start + 1 >= len(tokens) or tokens[start + 1].token_type is not TokenType.LT:
            continue
        angles = 0
        for end in range(start + 1, len(tokens)):
            kind = tokens[end].token_type
            if kind is TokenType.LT:
                angles += 1
            elif kind is TokenType.GT:
                angles -= 1
            elif kind not in _IN_ANGLES and not _is_type(tokens[end], types):
                break
            if angles == 0:
                found = _bad_type(tokens[start:end + 1], types)
                if found:
                    return found
                break
    return None


def _typed_literals(tokens: list, types) -> str | None:
    for index, item in enumerate(tokens[:-1]):
        following = tokens[index + 1]
        typed_literal = following.token_type is TokenType.STRING and _is_type(item, types)
        after_colons = _is_type(following, types) and item.token_type is TokenType.DCOLON
        if typed_literal and item.text.upper() not in BIGQUERY_TYPE_NAMES:
            return _reason(item)
        if after_colons and following.text.upper() not in BIGQUERY_TYPE_NAMES:
            return _reason(following)
    return None


def _definitions(statement: list, types) -> str | None:
    """The types a ``CREATE`` or ``DECLARE`` statement names outside angle brackets and casts."""

    if not statement:
        return None
    first = statement[0].text.upper()
    if first == "DECLARE":
        index = 1
        while index + 1 < len(statement) and statement[index + 1].token_type is TokenType.COMMA:
            index += 2
        if index + 1 < len(statement) and _is_type(statement[index + 1], types) and statement[index + 1].text.upper() not in BIGQUERY_TYPE_NAMES:
            return _reason(statement[index + 1])
        return None
    if first != "CREATE":
        return None
    kinds = {"TABLE", "FUNCTION", "PROCEDURE"}
    kind = next((i for i, item in enumerate(statement[:8]) if item.text.upper() in kinds), None)
    if kind is None:
        return None
    index = kind + 1
    while index < len(statement) and statement[index].token_type is not TokenType.L_PAREN:
        if statement[index].text.upper() in _NO_COLUMN_LIST:
            return None
        index += 1
    items: list[list] = []
    depth = 0
    end = len(statement)
    for position in range(index, len(statement)):
        item = statement[position]
        if item.token_type is TokenType.L_PAREN:
            depth += 1
            if depth == 1:
                items.append([])
                continue
        elif item.token_type is TokenType.R_PAREN:
            depth -= 1
            if depth == 0:
                end = position + 1
                break
        elif depth == 1 and item.token_type is TokenType.COMMA:
            items.append([])
            continue
        if items:
            items[-1].append(item)
    for definition in items:
        if definition and definition[0].text.upper() in {"IN", "OUT", "INOUT"} and len(definition) >= 3:
            definition = definition[1:]
        if len(definition) >= 2 and definition[0].text.upper() not in _NOT_COLUMNS:
            declared = definition[1]
            if _is_type(declared, types) and declared.text.upper() not in BIGQUERY_TYPE_NAMES | {"ANY TYPE"}:
                return _reason(declared)  # (ANY TYPE is a templated function's parameter, valid there)
    if end + 1 < len(statement) and statement[end].text.upper() == "RETURNS":
        returned = statement[end + 1]
        if _is_type(returned, types) and returned.text.upper() not in BIGQUERY_TYPE_NAMES:
            return _reason(returned)
    return None


def invalid_type_name(sql: str) -> str | None:
    """The first type name ``sql`` writes that BigQuery does not have, as a reason (see the module notes)."""

    try:
        tokens = tokenize(sql, read="bigquery")
    except Exception:  # noqa: BLE001 - text that cannot be tokenized is the parser's to refuse
        return None
    types = Parser.TYPE_TOKENS
    for check in (_casts, _angle_types, _typed_literals):
        found = check(tokens, types)
        if found:
            return found
    statement: list = []
    for token in [*tokens, None]:
        if token is not None and token.token_type is not TokenType.SEMICOLON:
            statement.append(token)
            continue
        if len(statement) >= 2 and statement[0].text.upper() == "BEGIN" and statement[1].token_type is TokenType.STRING:
            found = invalid_type_name(statement[1].text)  # the tokenizer reads a block's first statement as one string
        else:
            found = _definitions(statement, types)
        if found:
            return found
        statement = []
    return None
