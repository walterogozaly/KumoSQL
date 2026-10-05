"""Pipe query syntax that sqlglot reads into the wrong query, rewritten to a form it reads or refused.

sqlglot turns ``FROM t |> op |> op`` into one ``SELECT`` and keeps adding to it: ``|> WHERE`` fills the ``WHERE``, ``|> LIMIT`` the
``LIMIT``, ``|> JOIN`` a join. That is right only while the operators come in an order a ``SELECT`` can say. After a ``|> LIMIT``
a later ``|> WHERE``, ``|> ORDER BY`` or ``|> JOIN`` would be read as acting before the limit (``FROM t |> LIMIT 1 |> ORDER BY x``
became ``SELECT * FROM t ORDER BY x LIMIT 1``, a different row). Other operators sqlglot drops without a word: ``|> PIVOT`` (and ``|> UNPIVOT``, which it ran after the filters before it),
``|> SELECT DISTINCT``, the ``AS STRUCT`` and ``AS VALUE`` of ``|> SELECT``, the ordering of ``GROUP AND ORDER BY`` and the
``WINDOW`` clause of ``|> SELECT``; ``GROUP BY ROLLUP (x)`` of ``|> AGGREGATE`` is read as a column called ``ROLLUP``.

What this module does about each, working on the tokens so that it is the same on every sqlglot build:

* ``FROM t |> WHERE c |> PIVOT(...)`` (and ``UNPIVOT``) is the table ``PIVOT`` of the query before it, ``FROM (FROM t |> WHERE c) PIVOT(...)``;
* ``|> WINDOW e AS a`` is BigQuery's own spelling of ``|> EXTEND e AS a`` and is written that way;
* ``|> SELECT DISTINCT items`` is ``|> SELECT items |> DISTINCT``;
* ``GROUP AND ORDER BY a, b DESC`` is ``GROUP BY a ASC, b DESC``;
* everything else listed above, and any operator order that a single ``SELECT`` would read differently, is refused with a
  :class:`MisreadPipe` (a ``ParseError``): a query that cannot be read faithfully is reported unparseable, never misread.
"""

from __future__ import annotations

from sqlglot.errors import ParseError
from sqlglot.tokens import TokenType

_PIPE = getattr(TokenType, "PIPE_GT", None)  # sqlglot 26.0.0 has no pipe syntax at all
_OPEN = (TokenType.L_PAREN, TokenType.L_BRACKET)
_CLOSE = (TokenType.R_PAREN, TokenType.R_BRACKET)
_JOINS = ("JOIN", "LEFT", "RIGHT", "FULL", "INNER", "CROSS", "NATURAL", "OUTER")
_SET_OPERATORS = ("UNION", "INTERSECT", "EXCEPT")
# Operators after which sqlglot starts a new ``SELECT`` over the result, so nothing earlier can leak into what follows.
_BUILDS = ("SELECT", "EXTEND", "AGGREGATE", "AS", "SET", "DROP", "WINDOW", "PIVOT", "UNPIVOT", "CALL", "RENAME", *_SET_OPERATORS)


class MisreadPipe(ParseError):
    """A pipe query sqlglot would read as a different query."""


def has_pipe(tokens: list) -> bool:
    return _PIPE is not None and any(token.token_type == _PIPE for token in tokens)


def _segments(tokens: list) -> list[tuple[int, int, int]]:
    """``(chain, first, end)`` for every pipe operator: ``tokens[first]`` is its ``|>`` and ``tokens[first + 1:end]`` the operator.

    A chain is the operators that follow one another in one query: the same parenthesis level of one statement.
    """

    segments: list[tuple[int, int, int]] = []
    chains = 0
    stack: list[list[int | None]] = [[chains, None]]  # [chain id, index of the open pipe]

    def close(end: int) -> None:
        if stack[-1][1] is not None:
            segments.append((stack[-1][0], stack[-1][1], end))
            stack[-1][1] = None

    for index, token in enumerate(tokens):
        kind = token.token_type
        if kind in _OPEN:
            chains += 1
            stack.append([chains, None])
        elif kind in _CLOSE:
            close(index)
            if len(stack) > 1:
                stack.pop()
        elif kind == TokenType.SEMICOLON and len(stack) == 1:
            close(index)
            chains += 1
            stack[-1][0] = chains
        elif kind == _PIPE:
            close(index)
            stack[-1][1] = index
    for level in reversed(stack):
        if level[1] is not None:
            segments.append((level[0], level[1], len(tokens)))
    return sorted(segments, key=lambda segment: segment[1])


def _word(tokens: list, index: int) -> str:
    return " ".join(tokens[index].text.upper().split()) if index < len(tokens) else ""  # ``ORDER BY`` is one token


def _operator(tokens: list, first: int, end: int) -> str:
    """The operator a segment holds: its first word, ``JOIN`` for every spelling of a join, ``UNION`` for a set operation."""

    word = _word(tokens, first + 1)
    if word in _JOINS or word in ("INNER", "LEFT", "FULL"):
        for index in range(first + 1, end):
            if _word(tokens, index) == "JOIN":
                return "JOIN"
            if _word(tokens, index) in _SET_OPERATORS:
                return "UNION"
    return word


def _depths(tokens: list, start: int, end: int):
    """``(index, token)`` of the tokens of ``tokens[start:end]`` outside every bracket."""

    depth = 0
    for index in range(start, end):
        kind = tokens[index].token_type
        if kind in _CLOSE:
            depth -= 1
        if depth == 0:
            yield index, tokens[index]
        if kind in _OPEN:
            depth += 1


def _edit(sql: str, edits: list[tuple[int, int, str]]) -> str:
    pieces, position = [], 0
    for start, end, text in sorted(edits):
        pieces += [sql[position:start], text]
        position = end
    return "".join(pieces) + sql[position:]


def rewrite(sql: str, tokens: list) -> str:
    """``sql`` with the pipe operators that have a plain spelling written that way (see the module docstring)."""

    edits: list[tuple[int, int, str]] = []
    for _, first, end in _segments(tokens):
        word = _word(tokens, first + 1)
        if word in ("PIVOT", "UNPIVOT"):
            if not edits:  # one at a time: the text it leaves is read again, and the next one is rewritten then
                edit = _pivot_over_input(sql, tokens, first, end)
                if edit:
                    return _edit(sql, [edit])
        elif word == "WINDOW":
            edits.append((tokens[first + 1].start, tokens[first + 1].end + 1, "EXTEND"))
        elif word == "SELECT" and _word(tokens, first + 2) == "DISTINCT" and first + 3 < end:
            if _word(tokens, first + 3) == "AS" and _word(tokens, first + 4) in ("STRUCT", "VALUE"):
                continue  # refused by check()
            edits.append((tokens[first + 2].start, tokens[first + 2].end + 1, ""))
            edits.append((tokens[end - 1].end + 1, tokens[end - 1].end + 1, " |> DISTINCT"))
        elif word == "AGGREGATE":
            edit = _group_and_order_by(sql, tokens, first, end)
            if edit:
                edits.extend(edit)
    return _edit(sql, edits) if edits else sql


def _chain_start(tokens: list, index: int) -> int:
    """Index of the first token of the query that holds ``tokens[index]``: just after the parenthesis it sits in, or after
    the statement's start."""

    depth = 0
    for i in range(index - 1, -1, -1):
        kind = tokens[i].token_type
        if kind in _CLOSE:
            depth += 1
        elif kind in _OPEN:
            if depth == 0:
                return i + 1
            depth -= 1
        elif kind == TokenType.SEMICOLON and depth == 0:
            return i + 1
    return 0


def _pivot_over_input(sql: str, tokens: list, first: int, end: int) -> tuple[int, int, str] | None:
    """``FROM t |> WHERE c |> PIVOT(...) AS p`` as ``FROM (FROM t |> WHERE c) PIVOT(...) AS p``: BigQuery defines the pipe
    operator as the table ``PIVOT`` applied to the query before it. sqlglot dropped a ``|> PIVOT`` and put a ``|> UNPIVOT`` on the
    first table of the chain, so a filter before it ran after it."""

    start = _chain_start(tokens, first)
    if start >= first or end <= first + 2:
        return None
    head = tokens[start].start
    return head, tokens[end - 1].end + 1, f"FROM ({sql[head : tokens[first].start].strip()}) {sql[tokens[first + 1].start : tokens[end - 1].end + 1]}"


def _group_and_order_by(sql: str, tokens: list, first: int, end: int) -> list[tuple[int, int, str]]:
    """``GROUP AND ORDER BY a, b DESC`` as ``GROUP BY a ASC, b DESC``: sqlglot reads the ordering only when it is spelled out."""

    top = [index for index, _ in _depths(tokens, first + 1, end)]
    for position in top:
        if _word(tokens, position) == "GROUP" and [_word(tokens, position + n) for n in (1, 2)] == ["AND", "ORDER BY"]:
            break
    else:
        return []
    items: list[list[int]] = [[]]
    for index in top:
        if index < position + 3:
            continue
        if tokens[index].token_type == TokenType.COMMA:
            items.append([])
        else:
            items[-1].append(index)
    edits = [(tokens[position].start, tokens[position + 2].end + 1, "GROUP BY")]
    for item in items:
        if not item:
            return []
        words = [_word(tokens, index) for index in item]
        if words[-1] in ("ASC", "DESC") or (len(words) > 1 and words[-3:-1] in (["ASC", "NULLS"], ["DESC", "NULLS"])):
            continue
        if "NULLS" in words:
            return []  # check() refuses it
        edits.append((tokens[item[-1]].end + 1, tokens[item[-1]].end + 1, " ASC"))
    return edits


def check(sql: str, tokens: list) -> None:
    """Raise :class:`MisreadPipe` for a pipe query sqlglot would read as a different one."""

    chains: dict[int, list[tuple[int, int]]] = {}
    for chain, first, end in _segments(tokens):
        chains.setdefault(chain, []).append((first, end))
    for operators in chains.values():
        _check_chain(tokens, operators)


def _check_chain(tokens: list, operators: list[tuple[int, int]]) -> None:
    limited = distinct = filtered = sampled = False
    seen = 0  # operators since the last one that starts a new SELECT
    for position, (first, end) in enumerate(operators):
        word = _operator(tokens, first, end)
        text = " ".join(_word(tokens, index) for index, _ in _depths(tokens, first + 1, end))
        body = [(index, token) for index, token in _depths(tokens, first + 1, end)]
        if sampled and word not in _BUILDS:
            raise MisreadPipe("a pipe TABLESAMPLE followed by another operator that is not a new SELECT is not read faithfully")
        if word in ("PIVOT",):
            raise MisreadPipe("the pipe PIVOT operator is not read")
        if word == "SELECT":
            if text.startswith("SELECT AS STRUCT") or text.startswith("SELECT AS VALUE") or text.startswith("SELECT DISTINCT AS "):
                raise MisreadPipe("a pipe SELECT AS STRUCT or AS VALUE is not read")
            if any(_word(tokens, index) == "WINDOW" for index, _ in body):
                raise MisreadPipe("a WINDOW clause on a pipe SELECT is not read")
        if word == "AGGREGATE":
            _check_aggregate(tokens, first, end)
        if word in _BUILDS:
            limited = distinct = filtered = sampled = False
            seen = 0
            continue
        if word == "TABLESAMPLE":
            if position or seen or limited:
                raise MisreadPipe("a pipe TABLESAMPLE that is not the first operator is not read faithfully")
            sampled = True
        elif limited and word in ("WHERE", "ORDER BY", "LIMIT", "JOIN", "DISTINCT"):
            raise MisreadPipe(f"a pipe {word} after a pipe LIMIT would be read as acting before the limit")
        elif word == "JOIN":
            if distinct:
                raise MisreadPipe("a pipe JOIN after a pipe DISTINCT would be read as acting before it")
            kind = {_word(tokens, index) for index in range(first + 1, end)} & {"RIGHT", "FULL"}
            if filtered and kind:
                raise MisreadPipe("a pipe RIGHT or FULL JOIN after a pipe WHERE would be read as filtering the joined rows")
        elif word == "LIMIT":
            limited = True
        elif word == "DISTINCT":
            distinct = True
        elif word == "WHERE":
            filtered = True
        seen += 1


def _check_aggregate(tokens: list, first: int, end: int) -> None:
    for index in range(first + 1, end):
        word = _word(tokens, index)
        following = _word(tokens, index + 1)
        if (word in ("ROLLUP", "CUBE") and tokens[index + 1 : index + 2] and tokens[index + 1].token_type == TokenType.L_PAREN) or (
            word == "GROUPING" and following == "SETS"
        ):
            raise MisreadPipe("a pipe AGGREGATE with ROLLUP, CUBE or GROUPING SETS is not read")
    top = [_word(tokens, index) for index, _ in _depths(tokens, first + 1, end)]
    if "NULLS" in top and ("ASC" not in top and "DESC" not in top):
        raise MisreadPipe("a pipe AGGREGATE ordering with NULLS FIRST or LAST and no direction is not read")
