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
- any ``ARRAY<...>``, ``STRUCT<...>`` or ``RANGE<...>`` (a typed literal, a column or a parameter), including types
  nested in them; a struct field named like a type (``STRUCT<text STRING>``) is a name, not a type;
- a typed literal such as ``FLOAT '1'`` (a type keyword directly followed by a string);
- the column and parameter list of ``CREATE TABLE``, ``CREATE FUNCTION`` and ``CREATE PROCEDURE``, a ``RETURNS``
  type, ``RETURNS TABLE<...>`` and ``TABLE<...>`` parameters, an external table's ``WITH PARTITION COLUMNS``, the
  ``INPUT`` and ``OUTPUT`` lists of ``CREATE MODEL``, the type of a ``DECLARE``;
- ``ALTER TABLE ... ADD COLUMN`` and ``ALTER COLUMN ... SET DATA TYPE`` (and the shorter spellings sqlglot reads),
  and the column list of ``LOAD DATA``.

Script statements are read where they start: after a ``;`` and after ``THEN``, ``ELSE``, ``DO``, ``LOOP`` and
``BEGIN``. The tokenizer keeps the rest of a ``BEGIN``, ``EXCEPTION``, ``WHILE``, ``LOOP`` or ``REPEAT`` statement as
one string, which is read again as SQL.

It is a refusal list, not a validator. A word the tokenizer does not treat as a type (a user-defined name) is left
to the parser, which already refuses it. A bare type keyword anywhere else is not read, because a column or alias
may be spelled the same (``SELECT a float FROM t`` is valid), so reading it would refuse valid queries; so is a
statement held in a string (``EXECUTE IMMEDIATE '...'``).
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
_ANGLE_TYPES = {TokenType.ARRAY, TokenType.STRUCT, TokenType.RANGE}
_BODY_OPENERS = {"THEN", "ELSE", "DO", "LOOP", "BEGIN"}
_STRINGS = {TokenType.STRING, TokenType.RAW_STRING, TokenType.BYTE_STRING, TokenType.NATIONAL_STRING}
# What can sit between the angle brackets of a type: type words, field names, commas, nested brackets and the
# length or precision of a type. Anything else means the text is not a type after all.
_IN_ANGLES = {TokenType.VAR, TokenType.IDENTIFIER, TokenType.COMMA, TokenType.LT, TokenType.GT, TokenType.L_PAREN,
              TokenType.R_PAREN, TokenType.NUMBER}
_NOT_COLUMNS = {"CONSTRAINT", "PRIMARY", "PRIMARY KEY", "FOREIGN", "FOREIGN KEY", "UNIQUE", "CHECK"}
_NO_COLUMN_LIST = {"AS", "LIKE", "CLONE", "COPY", "SELECT", "WITH", "OPTIONS", "PARTITION", "CLUSTER", "RETURNS", "LANGUAGE", "FROM"}


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


def _angle_types(tokens: list, types, starts=_ANGLE_TYPES) -> str | None:
    for start, token in enumerate(tokens):
        if token.token_type not in starts or start + 1 >= len(tokens) or tokens[start + 1].token_type is not TokenType.LT:
            continue
        if start and tokens[start - 1].token_type is TokenType.DOT:
            continue  # a field or column that is named like the type: t.range < text
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
        typed_literal = following.token_type in _STRINGS and _is_type(item, types)
        after_colons = _is_type(following, types) and item.token_type is TokenType.DCOLON
        if typed_literal and item.text.upper() not in BIGQUERY_TYPE_NAMES:
            return _reason(item)
        if after_colons and following.text.upper() not in BIGQUERY_TYPE_NAMES:
            return _reason(following)
    return None


def _is_unknown(item, types, extra=frozenset()) -> bool:
    return _is_type(item, types) and item.text.upper() not in BIGQUERY_TYPE_NAMES | extra


def _group(statement: list, open_index: int) -> tuple[list[list], int]:
    """The comma separated items of the parenthesised list that opens at ``open_index``, and where it ends."""

    items: list[list] = []
    depth = 0
    for position in range(open_index, len(statement)):
        item = statement[position]
        if item.token_type is TokenType.L_PAREN:
            depth += 1
            if depth == 1:
                items.append([])
                continue
        elif item.token_type is TokenType.R_PAREN:
            depth -= 1
            if depth == 0:
                return items, position + 1
        elif depth == 1 and item.token_type is TokenType.COMMA:
            items.append([])
            continue
        if items:
            items[-1].append(item)
    return items, len(statement)


def _list_types(items: list[list], types) -> str | None:
    """The first unknown type in a list of ``name type ...`` definitions (a column, a parameter, a model field)."""

    for definition in items:
        if definition and definition[0].text.upper() in {"IN", "OUT", "INOUT"} and len(definition) >= 3:
            definition = definition[1:]
        if len(definition) >= 2 and definition[0].text.upper() not in _NOT_COLUMNS:
            declared = definition[1]
            if _is_unknown(declared, types, {"ANY TYPE"}):  # (ANY TYPE is a templated function's parameter, valid there)
                return _reason(declared)
    return None


def _first_list(statement: list, start: int) -> int | None:
    """Where the column list that follows the name in ``statement[start:]`` opens, if there is one."""

    index = start
    while index < len(statement) and statement[index].token_type is not TokenType.L_PAREN:
        if statement[index].text.upper() in _NO_COLUMN_LIST:
            return None
        index += 1
    return index if index < len(statement) else None


def _lists_after(statement: list, types, keywords: set[str], first: int = 0) -> str | None:
    """The unknown types in every parenthesised list that follows one of ``keywords`` (``INPUT (...)``)."""

    for index in range(first, len(statement) - 1):
        if statement[index].text.upper() in keywords and statement[index + 1].token_type is TokenType.L_PAREN:
            found = _list_types(_group(statement, index + 1)[0], types)
            if found:
                return found
    return None


def _alter(statement: list, types) -> str | None:
    """The types an ``ALTER TABLE`` names: ``ADD COLUMN [IF NOT EXISTS] name TYPE`` and ``ALTER COLUMN [IF EXISTS]
    name SET DATA TYPE TYPE``. sqlglot also reads ``ADD name TYPE``, ``ADD COLUMNS (name TYPE, ...)`` and an
    ``ALTER COLUMN`` without ``SET DATA`` or ``TYPE``, and prints each as the full BigQuery form, so the type is read
    after any of those spellings."""

    words = [item.text.upper() for item in statement]

    def after(start: int, *skip: tuple[str, ...]) -> int:
        for option in skip:  # an optional keyword sequence
            if words[start:start + len(option)] == list(option):
                start += len(option)
        return start

    for index, word in enumerate(words):
        if word == "ADD":
            start = after(index + 1, ("COLUMNS",), ("COLUMN",), ("IF", "NOT", "EXISTS"))
            if start < len(statement) and statement[start].token_type is TokenType.L_PAREN:
                found = _list_types(_group(statement, start)[0], types)
                if found:
                    return found
                continue
            position = start + 1  # the type follows the column name
        elif word == "ALTER" and words[index + 1:index + 2] == ["COLUMN"]:
            start = after(index + 2, ("IF", "EXISTS"))
            position = after(start + 1, ("SET", "DATA"), ("TYPE",))
        else:
            continue
        if position < len(statement) and _is_unknown(statement[position], types):
            return _reason(statement[position])
    return None


def _definitions(statement: list, types) -> str | None:
    """The types a statement names outside angle brackets and casts: ``DECLARE``, the column and parameter lists of
    ``CREATE``, ``ALTER TABLE ... ADD COLUMN`` and ``LOAD DATA``."""

    if not statement:
        return None
    first = statement[0].text.upper()
    if first == "DECLARE":
        index = 1
        while index + 1 < len(statement) and statement[index + 1].token_type is TokenType.COMMA:
            index += 2
        if index + 1 < len(statement) and _is_unknown(statement[index + 1], types):
            return _reason(statement[index + 1])
        return None
    if first == "ALTER" and len(statement) > 1 and statement[1].text.upper() == "TABLE":
        return _alter(statement, types)
    if first == "LOAD" and len(statement) > 1 and statement[1].text.upper() == "DATA":
        opened = _first_list(statement, 2)
        return _list_types(_group(statement, opened)[0], types) if opened is not None else None
    if first != "CREATE":
        return None
    kinds = {"TABLE", "FUNCTION", "PROCEDURE", "MODEL"}
    kind = next((i for i, item in enumerate(statement[:8]) if item.text.upper() in kinds), None)
    if kind is None:
        return None
    if statement[kind].text.upper() == "MODEL":
        return _lists_after(statement, types, {"INPUT", "OUTPUT"}, kind)
    if any(item.text.upper() == "FUNCTION" for item in statement[:kind + 2]):
        # a table function's RETURNS TABLE<a FLOAT> and a TABLE<...> parameter (TABLE is a name elsewhere, so this
        # is read only here)
        found = _angle_types(statement, types, {TokenType.TABLE})
        if found:
            return found
    opened = _first_list(statement, kind + 1)
    end = len(statement)
    if opened is not None:
        items, end = _group(statement, opened)
        found = _list_types(items, types)
        if found:
            return found
    if statement[kind].text.upper() == "TABLE":
        for index in range(kind, len(statement) - 2):  # an external table's WITH PARTITION COLUMNS (name type, ...)
            if [item.text.upper() for item in statement[index:index + 2]] == ["PARTITION", "COLUMNS"]:
                found = _list_types(_group(statement, index + 2)[0], types)
                if found:
                    return found
    if opened is not None and end + 1 < len(statement) and statement[end].text.upper() == "RETURNS":
        returned = statement[end + 1]
        if _is_unknown(returned, types):
            return _reason(returned)
    return None


def _statements(tokens: list):
    """Every statement of a script, each ``THEN``, ``ELSE``, ``DO``, ``LOOP`` and ``BEGIN`` body included (a
    statement is read from its first word on, so a body is read as the statement it is)."""

    statement: list = []
    for token in [*tokens, None]:
        if token is not None and token.token_type is not TokenType.SEMICOLON:
            statement.append(token)
            continue
        yield statement
        for index, item in enumerate(statement):
            if item.text.upper() in _BODY_OPENERS:
                yield statement[index + 1:]
        statement = []


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
    for index, token in enumerate(tokens[:-1]):
        if token.token_type is TokenType.COMMAND and tokens[index + 1].token_type is TokenType.STRING:
            # The tokenizer keeps the rest of a BEGIN, EXCEPTION, WHILE, LOOP or REPEAT statement as one string.
            found = invalid_type_name(tokens[index + 1].text)
            if found:
                return found
    for statement in _statements(tokens):
        found = _definitions(statement, types)
        if found:
            return found
    return None
