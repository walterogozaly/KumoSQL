"""A parenthesized join that starts with ``UNNEST``: ``FROM (UNNEST([1, 2]) AS x JOIN t ON x = t.a)``.

BigQuery allows it (checked with a dry run) and reads the parentheses as plain grouping: the columns of ``x`` and ``t`` are visible
outside, and the group may be joined to anything after it. sqlglot reads ``(t1 JOIN t2 ON ...)`` as a subquery around a table that
carries the joins, but its ``UNNEST`` parser returns before joins are looked for, so a group that *starts* with ``UNNEST`` stops at
the ``JOIN`` ("Expecting )") and the model was unreadable.

Here the first item is swapped for a marker table function holding the item's text, sqlglot reads the group as it reads any other,
and the marker is replaced by the ``UNNEST`` node it stands for. The result is the same shape sqlglot gives ``(t1 JOIN t2 ...)``
(``Subquery`` around a ``Table`` with ``joins``), except that the ``Table`` holds the ``Unnest`` as its ``this``. It prints back as
written, every table joined in the group is an ordinary ``Table`` read, and a rule that wants a named base table finds none
(``alias_or_name`` is empty) and leaves the group alone.

Only what BigQuery accepts is read. The group must hold at least one join after the ``UNNEST`` item (``(UNNEST(a) AS x)`` and a comma
join inside the group are syntax errors there), the item must be an ``UNNEST`` call with an optional alias and ``WITH OFFSET``, and
a group followed by an alias (``(UNNEST(a) AS x JOIN t ON c) AS j``) is rejected by BigQuery, so it stays a parse error here.
Anything else is left as sqlglot raised it: refused, never guessed at.

This module is called from ``bigquery_syntax`` (which owns the retry after a ``ParseError``), so it works the same on pure and
compiled sqlglot, whose parser methods and expression classes cannot be replaced.
"""

from __future__ import annotations

from sqlglot import exp
from sqlglot.errors import ParseError
from sqlglot.tokens import TokenType

UNNEST_FIRST = "__KUMO_UNNEST_FIRST__"

_JOIN_WORDS = ("JOIN", "LEFT", "RIGHT", "FULL", "INNER", "CROSS", "NATURAL")
# Tokens that can precede a parenthesized table group; the opening parenthesis of a call (``f(UNNEST(...) ...``) is not one.
_TABLE_POSITION_WORDS = ("JOIN", "FROM")


def _is_join_start(token) -> bool:
    return token.token_type == TokenType.JOIN or token.text.upper() in _JOIN_WORDS


def _group_pairs(tokens: list) -> dict[int, int]:
    stack: list[int] = []
    pairs: dict[int, int] = {}
    for index, token in enumerate(tokens):
        if token.token_type == TokenType.L_PAREN:
            stack.append(index)
        elif token.token_type == TokenType.R_PAREN and stack:
            pairs[stack.pop()] = index
    return pairs


def _in_table_position(tokens: list, opened: int) -> bool:
    """Whether the ``(`` at ``opened`` can open a table group: after ``FROM``, a join keyword, a comma or another group's ``(``."""

    if opened == 0:
        return False
    before = tokens[opened - 1]
    return (
        before.token_type in (TokenType.FROM, TokenType.COMMA, TokenType.L_PAREN) or before.text.upper() in _TABLE_POSITION_WORDS
    )


def _first_item_end(tokens: list, start: int, close: int) -> int | None:
    """The index of the first join keyword that follows the ``UNNEST`` item starting at ``start``, at the group's own depth."""

    depth = 0
    for index in range(start, close):
        kind = tokens[index].token_type
        if kind in (TokenType.L_PAREN, TokenType.L_BRACKET):
            depth += 1
        elif kind in (TokenType.R_PAREN, TokenType.R_BRACKET):
            depth -= 1
        elif depth == 0 and kind == TokenType.COMMA:
            return None
        elif depth == 0 and index > start and _is_join_start(tokens[index]):
            return index
    return None


def rewrite_unnest_join_groups(sql: str, tokens: list) -> str:
    """``(UNNEST(a) AS x JOIN t ON c)`` becomes ``(__KUMO_UNNEST_FIRST__('UNNEST(a) AS x') JOIN t ON c)``."""

    from .bigquery_syntax import _apply_edits, _quoted

    pairs = _group_pairs(tokens)
    edits: list[tuple[int, int, str]] = []
    for opened, close in pairs.items():
        first = opened + 1
        if (
            first + 1 >= close or tokens[first].text.upper() != "UNNEST" or tokens[first + 1].token_type != TokenType.L_PAREN
            or not _in_table_position(tokens, opened)
        ):
            continue
        end = _first_item_end(tokens, first, close)
        after = tokens[close + 1] if close + 1 < len(tokens) else None
        if end is None or (after is not None and after.token_type in (TokenType.ALIAS, TokenType.VAR, TokenType.IDENTIFIER)):
            continue  # a group that BigQuery rejects stays the error sqlglot raised
        item = sql[tokens[first].start : tokens[end - 1].end + 1]
        edits.append((tokens[first].start, tokens[end - 1].end + 1, f"{UNNEST_FIRST}({_quoted(item)})"))
    return _apply_edits(sql, edits)


def _first_item(call: exp.Anonymous) -> exp.Unnest:
    """The ``UNNEST`` item a marker holds, read again on its own; anything but a bare ``UNNEST`` source is refused."""

    from sqlglot.dialects.bigquery import BigQuery

    arguments = call.expressions
    if len(arguments) != 1 or not arguments[0].is_string:
        raise ParseError("a parenthesized join that starts with UNNEST is not read: the first item is not an UNNEST call")
    item = BigQuery().parse(f"SELECT * FROM {arguments[0].this}")
    select = item[0] if len(item) == 1 else None
    source = select.args.get("from_") or select.args.get("from") if isinstance(select, exp.Select) else None
    unnest = source.this if isinstance(source, exp.From) else None
    if not isinstance(unnest, exp.Unnest) or unnest.args.get("joins") or unnest.args.get("explode_array"):
        raise ParseError("a parenthesized join that starts with UNNEST is not read: the first item is not an UNNEST call")
    unnest.parent = None
    return unnest


def resolve_unnest_join_groups(tree: exp.Expression) -> exp.Expression:
    """Replace each marker table function with the ``Unnest`` it holds; a marker anywhere else is refused."""

    for call in list(tree.find_all(exp.Anonymous)):
        if call.name != UNNEST_FIRST:
            continue
        table = call.parent
        if not isinstance(table, exp.Table) or table.this is not call or not table.args.get("joins") or table.args.get("alias"):
            raise ParseError("a parenthesized join that starts with UNNEST is not read: it is not a group of joined tables")
        group = table.parent
        if isinstance(group, exp.Subquery) and group.args.get("alias"):
            raise ParseError("a parenthesized join that starts with UNNEST is not read: BigQuery rejects an alias after the group")
        call.replace(_first_item(call))
    return tree
