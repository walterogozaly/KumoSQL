"""Refuse a proof whose input names a type BigQuery does not have.

sqlglot reads many other engines' type names and prints them as BigQuery types: ``FLOAT``, ``REAL`` and
``DOUBLE`` become ``FLOAT64``, ``INT32`` and ``INT8`` become ``INT64``, ``VARCHAR``, ``TEXT``, ``CHAR`` and
``UUID`` become ``STRING``, ``BINARY`` becomes ``BYTES``. BigQuery rejects every one of them ("Type not found"),
so a proof of ``CAST(x AS FLOAT)`` against ``CAST(x AS FLOAT64)`` credits a pair that has nothing to preserve,
and the parsed trees can no longer tell the two apart. The names are only visible in the source text, so this
module reads the tokens of ``CAST`` and ``SAFE_CAST`` (the one place a query names a type) and checks each type
keyword in the target type, including those nested in ``ARRAY<...>``, ``STRUCT<...>`` and ``RANGE<...>``,
against the names BigQuery accepts. Every name in the list was accepted by a BigQuery dry run (2026-10-04;
the others, including those above, returned "Type not found").

It is a refusal list, not a validator: a type written in a place other than a cast (a table definition, a
function signature) is not read, and a word the tokenizer does not treat as a type (a user-defined name) is
left to the parser, which already refuses it.
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

_CASTS = {"CAST", "SAFE_CAST"}


def invalid_type_name(sql: str) -> str | None:
    """The first type name in a ``CAST`` or ``SAFE_CAST`` of ``sql`` that BigQuery does not have, as a reason."""

    try:
        tokens = tokenize(sql, read="bigquery")
    except Exception:  # noqa: BLE001 - text that cannot be tokenized is the parser's to refuse
        return None
    type_tokens = Parser.TYPE_TOKENS
    for start, token in enumerate(tokens):
        if token.text.upper() not in _CASTS or start + 1 >= len(tokens) or tokens[start + 1].token_type is not TokenType.L_PAREN:
            continue
        depth = 0
        in_type = False
        for item in tokens[start + 1:]:
            if item.token_type is TokenType.L_PAREN:
                depth += 1
            elif item.token_type is TokenType.R_PAREN:
                depth -= 1
                if depth == 0:
                    break
            elif depth == 1 and item.token_type is TokenType.ALIAS:
                in_type = True
            elif in_type and item.token_type is TokenType.FORMAT:
                break
            elif in_type and item.token_type in type_tokens and item.text.upper() not in BIGQUERY_TYPE_NAMES:
                return f"the type name {item.text.upper()} does not exist in BigQuery (it reports \"Type not found\")"
    return None
