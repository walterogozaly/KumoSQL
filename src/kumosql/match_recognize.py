"""BigQuery's ``MATCH_RECOGNIZE`` clause, read the way BigQuery reads it.

``SELECT * FROM t MATCH_RECOGNIZE (PARTITION BY p ORDER BY ts MEASURES FIRST(v) AS fv AFTER MATCH SKIP PAST LAST ROW PATTERN (A+)
DEFINE A AS v > 1 OPTIONS (...)) AS m`` is a table operator: it reads rows of the table or subquery before it and returns the
partition columns followed by the ``MEASURES``, and nothing else. sqlglot's BigQuery tokenizer has no ``MATCH_RECOGNIZE`` keyword, so
every spelling stopped at ``Expecting )``; its parser reads the clause (as ``exp.MatchRecognize``, in the ``match`` argument of a
table or subquery) once the word is a keyword, and that is all this module does for the parse, the same on pure and compiled builds:
the word is retyped in the token list before the parser sees it. The one thing sqlglot cannot hold is ``OPTIONS (...)`` after
``DEFINE``; it is written into the last ``DEFINE`` expression as a marker call that prints back as written.

BigQuery's grammar is narrower than the standard's, and only what BigQuery accepts is read (checked by dry run)::

    MATCH_RECOGNIZE ( [PARTITION BY ...] ORDER BY ... MEASURES ... [AFTER MATCH SKIP PAST LAST ROW | TO NEXT ROW]
                      PATTERN (...) DEFINE ... [OPTIONS (...)] ) [[AS] alias]

``ONE ROW PER MATCH``, ``ALL ROWS PER MATCH``, ``SKIP TO FIRST|LAST``, ``FINAL`` and ``RUNNING`` are syntax errors there, a clause
without ``ORDER BY``, ``MEASURES``, ``PATTERN`` or ``DEFINE`` is too, and so are they here: reading them would let a prover treat
SQL BigQuery rejects as a query. The pipe form ``|> MATCH_RECOGNIZE (...)`` is the same operator on the query before it and is
written as ``SELECT * FROM (that query) MATCH_RECOGNIZE (...)``.
"""

from __future__ import annotations

from sqlglot import exp
from sqlglot.errors import ParseError
from sqlglot.tokens import Token, TokenType


MATCH_OPTIONS = "__KUMO_MATCH_OPTIONS__"

_KEYWORD = getattr(TokenType, "MATCH_RECOGNIZE", None)  # every release has it, but only the Oracle, Presto and Snowflake tokenizers use it
_PIPE = getattr(TokenType, "PIPE_GT", None)  # sqlglot 26.0.0 has no pipe syntax at all
_CLAUSE_START = ("PARTITION", "ORDER", "MEASURES", "AFTER", "PATTERN", "DEFINE", "ONE", "ALL")
# What may stand just before the word for it to be a table operator and not a function call or a column: the end of a table name,
# an alias or a subquery.
_AFTER_TABLE = (TokenType.R_PAREN, TokenType.VAR, TokenType.IDENTIFIER, TokenType.TABLE_ALIAS if hasattr(TokenType, "TABLE_ALIAS") else TokenType.VAR)


def _is_keyword(token: Token) -> bool:
    return token.token_type == TokenType.VAR and token.text.upper() == "MATCH_RECOGNIZE" or token.token_type == _KEYWORD


def _closing(tokens: list, opened: int) -> int | None:
    depth = 0
    for index in range(opened, len(tokens)):
        kind = tokens[index].token_type
        if kind == TokenType.L_PAREN:
            depth += 1
        elif kind == TokenType.R_PAREN:
            depth -= 1
            if depth == 0:
                return index
    return None


def _clauses(tokens: list, opened: int, closed: int) -> list[tuple[str, int]]:
    """(keyword, token index) for each clause keyword of the body that sits outside every nested parenthesis, in order."""

    found: list[tuple[str, int]] = []
    depth = 0
    index = opened + 1
    while index < closed:
        token = tokens[index]
        kind, word = token.token_type, token.text.upper()
        if kind in (TokenType.L_PAREN, TokenType.L_BRACKET):
            depth += 1
        elif kind in (TokenType.R_PAREN, TokenType.R_BRACKET):
            depth -= 1
        elif depth == 0:
            if kind == TokenType.PARTITION_BY:
                found.append(("PARTITION BY", index))
            elif kind == TokenType.ORDER_BY:
                found.append(("ORDER BY", index))
            elif word == "MEASURES" and kind == TokenType.VAR:
                found.append(("MEASURES", index))
            elif word == "AFTER" and kind == TokenType.VAR and _words(tokens, index, "AFTER", "MATCH", "SKIP"):
                found.append(("AFTER", index))
            elif word == "PATTERN" and kind == TokenType.VAR and tokens[index + 1].token_type == TokenType.L_PAREN:
                found.append(("PATTERN", index))
            elif word == "DEFINE" and kind == TokenType.VAR:
                found.append(("DEFINE", index))
            elif word == "OPTIONS" and kind == TokenType.VAR and tokens[index + 1].token_type == TokenType.L_PAREN:
                found.append(("OPTIONS", index))
            elif (word, kind) in (("ONE", TokenType.VAR), ("ALL", TokenType.ALL)) or word in ("FINAL", "RUNNING"):
                found.append((word, index))
        index += 1
    return found


def _words(tokens: list, index: int, *words: str) -> bool:
    return all(
        index + offset < len(tokens) and tokens[index + offset].text.upper() == word for offset, word in enumerate(words)
    )


_ORDER = ("PARTITION BY", "ORDER BY", "MEASURES", "AFTER", "PATTERN", "DEFINE", "OPTIONS")


def _check_body(tokens: list, opened: int, closed: int) -> None:
    """Refuse a body BigQuery refuses, so that it is never read as a query."""

    found = _clauses(tokens, opened, closed)
    names = [name for name, _ in found]
    for rejected in ("ONE", "ALL", "FINAL", "RUNNING"):
        if rejected in names:
            raise ParseError(f"MATCH_RECOGNIZE: BigQuery rejects {rejected} here; it has no ROWS PER MATCH, FINAL or RUNNING")
    positions = [_ORDER.index(name) for name in names]
    if positions != sorted(positions) or len(set(positions)) != len(positions):
        raise ParseError("MATCH_RECOGNIZE clauses are out of order or repeated; BigQuery's order is PARTITION BY, ORDER BY, MEASURES, AFTER MATCH SKIP, PATTERN, DEFINE, OPTIONS")
    for needed in ("ORDER BY", "MEASURES", "PATTERN", "DEFINE"):
        if needed not in names:
            raise ParseError(f"MATCH_RECOGNIZE without {needed}: BigQuery rejects it")
    if "AFTER" in names:
        start = dict(found)["AFTER"]
        if not (_words(tokens, start, "AFTER", "MATCH", "SKIP", "PAST", "LAST", "ROW") or _words(tokens, start, "AFTER", "MATCH", "SKIP", "TO", "NEXT", "ROW")):
            raise ParseError("MATCH_RECOGNIZE: BigQuery has only AFTER MATCH SKIP PAST LAST ROW and TO NEXT ROW")


def _starts_clause(tokens: list, index: int) -> int | None:
    """The index of the ``)`` that closes the clause when ``tokens[index]`` is a ``MATCH_RECOGNIZE (`` table operator."""

    if index == 0 or index + 2 >= len(tokens) or not _is_keyword(tokens[index]) or tokens[index + 1].token_type != TokenType.L_PAREN:
        return None
    before = tokens[index - 1]
    if before.token_type not in _AFTER_TABLE and before.token_type != _PIPE:
        return None
    first = tokens[index + 2]
    if first.token_type not in (TokenType.PARTITION_BY, TokenType.ORDER_BY) and first.text.upper() not in _CLAUSE_START:
        return None
    return _closing(tokens, index + 1)


def mark_tokens(tokens: list) -> list:
    """The tokens with every ``MATCH_RECOGNIZE (`` table operator retyped as sqlglot's keyword, which its parser reads.

    Raises ``ParseError`` for a clause BigQuery would reject.
    """

    if _KEYWORD is None:
        return tokens
    out: list = []
    changed = False
    for index, token in enumerate(tokens):
        closed = _starts_clause(tokens, index)
        if closed is not None and token.token_type != _PIPE:
            _check_body(tokens, index + 1, closed)
            token = Token(_KEYWORD, token.text, line=token.line, col=token.col, start=token.start, end=token.end)
            changed = True
        out.append(token)
    return out if changed else tokens


_IDENTIFIER = (TokenType.VAR, TokenType.IDENTIFIER)
_PATH_JOINER = (TokenType.DOT, TokenType.DASH)  # ``p.d.t`` and the dashed project of ``my-project.d.t``
_NOT_AN_ALIAS = ("WINDOW", "QUALIFY", "OFFSET", "WITH", "FOR", "UNION", "INTERSECT", "EXCEPT")
# What may follow a clause that is the whole of its statement or of a parenthesized query.
_QUERY_END = (TokenType.SEMICOLON, TokenType.R_PAREN)


def _ident(token: Token) -> bool:
    return token.token_type in _IDENTIFIER and token.text.upper() != "MATCH_RECOGNIZE"


def _opening(tokens: list, closed: int) -> int | None:
    depth = 0
    for index in range(closed, -1, -1):
        kind = tokens[index].token_type
        if kind == TokenType.R_PAREN:
            depth += 1
        elif kind == TokenType.L_PAREN:
            depth -= 1
            if depth == 0:
                return index
    return None


def _path_start(tokens: list, last: int) -> int:
    start = last
    while start >= 2 and tokens[start - 1].token_type in _PATH_JOINER and _ident(tokens[start - 2]):
        start -= 2
    return start


def _operand(tokens: list, keyword: int) -> int | None:
    """The index of the first token of the table or subquery (with its alias) that the clause at ``keyword`` applies to."""

    j = keyword - 1
    if j >= 2 and _ident(tokens[j]) and tokens[j - 1].token_type == TokenType.ALIAS:
        j -= 2
    elif j >= 1 and _ident(tokens[j]) and (tokens[j - 1].token_type == TokenType.R_PAREN or _ident(tokens[j - 1])):
        j -= 1
    if j < 0:
        return None
    if tokens[j].token_type == TokenType.R_PAREN:
        start = _opening(tokens, j)
        if start is None:
            return None
        if start >= 1 and _ident(tokens[start - 1]):  # a call: ``dataset.fn(...)``
            start = _path_start(tokens, start - 1)
    elif _ident(tokens[j]):
        start = _path_start(tokens, j)
    else:
        return None
    before = tokens[start - 1].token_type if start >= 1 else None
    if before not in (TokenType.FROM, TokenType.JOIN, TokenType.COMMA, TokenType.L_PAREN):
        return None
    return start


def _trailing_alias(tokens: list, closed: int) -> int:
    """The index of the last token of the alias that follows the clause (``AS m`` or ``m``), or ``closed`` when there is none."""

    after = closed + 1
    if after + 1 < len(tokens) and tokens[after].token_type == TokenType.ALIAS and _ident(tokens[after + 1]):
        return after + 1
    if after < len(tokens) and _ident(tokens[after]) and tokens[after].text.upper() not in _NOT_AN_ALIAS:
        return after
    return closed


def _canonical(tokens: list, start: int, closed: int, alias_end: int) -> bool:
    """``SELECT * FROM operand MATCH_RECOGNIZE (...)`` that is the whole of its query: already the shape the rewrite makes."""

    if start < 3 or alias_end != closed:
        return False
    if [tokens[start - 3].token_type, tokens[start - 2].token_type, tokens[start - 1].token_type] != [TokenType.SELECT, TokenType.STAR, TokenType.FROM]:
        return False
    if start - 3 != _query_start(tokens, start - 3) and not (start - 4 >= 0 and tokens[start - 4].token_type == TokenType.L_PAREN):
        return False
    return closed + 1 >= len(tokens) or tokens[closed + 1].token_type in _QUERY_END


def _query_start(tokens: list, index: int) -> int:
    depth = 0
    for i in range(index - 1, -1, -1):
        kind = tokens[i].token_type
        if kind in (TokenType.R_PAREN, TokenType.R_BRACKET):
            depth += 1
        elif kind in (TokenType.L_PAREN, TokenType.L_BRACKET):
            if depth == 0:
                return i + 1
            depth -= 1
        elif kind == TokenType.SEMICOLON and depth == 0:
            return i + 1
    return 0


def rewrite_text(sql: str, tokens: list) -> str:
    """The text with ``OPTIONS (...)`` moved into the last ``DEFINE`` expression, or, when there are none left, every clause
    written as ``(SELECT * FROM operand MATCH_RECOGNIZE (...)) alias`` and a pipe ``|> MATCH_RECOGNIZE`` as the table operator
    it is.

    sqlglot reads the clause into the ``match`` of the whole ``SELECT``, wherever it stood: with a join or a comma next to it, or
    a ``WHERE`` after it, nothing says which relation it applies to, and it prints back in a different place. As the only
    thing in a query of its own the operand is unambiguous.
    """

    if _KEYWORD is None:
        return sql
    clauses = [(index, closed) for index in range(len(tokens)) if (closed := _starts_clause(tokens, index)) is not None]
    if not clauses:
        return sql
    for index, closed in clauses:
        _check_body(tokens, index + 1, closed)
    edits: list[tuple[int, int, str]] = []
    for index, closed in clauses:
        found = _clauses(tokens, index + 1, closed)
        if "OPTIONS" in [name for name, _ in found]:
            edits.append(_options_edit(sql, tokens, found, closed))
    if edits:
        return _apply(sql, edits)
    for index, closed in clauses:
        body = sql[tokens[index].start : tokens[closed].end + 1]
        if tokens[index - 1].token_type == _PIPE:
            start = _query_start(tokens, index - 1)
            edits.append((tokens[start].start, tokens[closed].end + 1, f"SELECT * FROM ({sql[tokens[start].start : tokens[index - 2].end + 1]}) {body}"))
            continue
        start = _operand(tokens, index)
        if start is None:
            raise ParseError("MATCH_RECOGNIZE follows something other than a table or a subquery")
        alias_end = _trailing_alias(tokens, closed)
        if _canonical(tokens, start, closed, alias_end):
            continue
        operand = sql[tokens[start].start : tokens[index - 1].end + 1]
        alias = sql[tokens[closed].end + 1 : tokens[alias_end].end + 1]
        edits.append((tokens[start].start, tokens[alias_end].end + 1, f"(SELECT * FROM {operand} {body}){alias}"))
    return _apply_nested(sql, edits)


def _apply_nested(sql: str, edits: list[tuple[int, int, str]]) -> str:
    """The edits that do not sit inside another one: a clause inside the operand of an outer clause is taken on the next pass."""

    kept = [e for e in edits if not any(o is not e and o[0] <= e[0] and e[1] <= o[1] for o in edits)]
    return _apply(sql, kept)


def _options_edit(sql: str, tokens: list, found: list[tuple[str, int]], closed: int) -> tuple[int, int, str]:
    define = dict(found)["DEFINE"]
    options = dict(found)["OPTIONS"]
    item = define + 1
    depth = 0
    for position in range(define + 1, options):
        kind = tokens[position].token_type
        if kind in (TokenType.L_PAREN, TokenType.L_BRACKET):
            depth += 1
        elif kind in (TokenType.R_PAREN, TokenType.R_BRACKET):
            depth -= 1
        elif kind == TokenType.COMMA and depth == 0:
            item = position + 1
    if item + 2 >= options or tokens[item + 1].token_type != TokenType.ALIAS:
        raise ParseError("MATCH_RECOGNIZE DEFINE item is not `name AS expression`")
    expression = sql[tokens[item + 2].start : tokens[options - 1].end + 1]
    closing = _closing(tokens, options + 1)
    options_end = tokens[closing].end + 1 if closing is not None else tokens[closed].start
    text = sql[tokens[options].start : options_end]
    return tokens[item + 2].start, options_end, f"{MATCH_OPTIONS}({expression}, {_quoted(text)})"


def _quoted(text: str) -> str:
    escaped = text.replace("\\", "\\\\").replace("'", "\\'").replace("\n", "\\n").replace("\r", "\\r")
    return f"'{escaped}'"


def _apply(sql: str, edits: list[tuple[int, int, str]]) -> str:
    pieces: list[str] = []
    position = 0
    for start, end, text in sorted(edits):
        pieces += [sql[position:start], text]
        position = end
    return "".join(pieces) + sql[position:] if pieces else sql


def generate_options(generate, expression: exp.Anonymous) -> str | None:
    """``__KUMO_MATCH_OPTIONS__(expr, 'OPTIONS (...)')`` as ``expr OPTIONS (...)``."""

    if expression.name == MATCH_OPTIONS and len(expression.expressions) == 2 and expression.expressions[1].is_string:
        return f"{generate(expression.expressions[0])} {expression.expressions[1].this}"
    return None


def _from_of(select: exp.Select) -> exp.Expression | None:
    return select.args.get("from_") or select.args.get("from")


def is_match_select(node: exp.Expression) -> bool:
    """``SELECT * FROM operand MATCH_RECOGNIZE (...)`` and nothing else: the one shape the clause is read in."""

    if not isinstance(node, exp.Select) or not node.args.get("match"):
        return False
    source = _from_of(node)
    star = node.args.get("expressions")
    others = {key for key, value in node.args.items() if value not in (None, [], False) and key not in ("expressions", "from", "from_", "match")}
    return source is not None and isinstance(source.this, (exp.Table, exp.Subquery)) and bool(star) and len(star) == 1 and isinstance(star[0], exp.Star) and not others


def matches(tree: exp.Expression) -> list[exp.MatchRecognize]:
    return list(tree.find_all(exp.MatchRecognize))


def generate_match_recognize(generator, expression: exp.MatchRecognize) -> str:
    text = type(generator).matchrecognize_sql(generator, expression) if hasattr(type(generator), "matchrecognize_sql") else ""
    return text.replace("MATCH_RECOGNIZE ( ", "MATCH_RECOGNIZE (", 1)


def generate_subquery(generator, expression: exp.Subquery, fallback) -> str:
    """The wrapper this module makes around an operand prints as it was written: ``operand MATCH_RECOGNIZE (...) alias``."""

    inner = expression.this
    if (
        isinstance(expression.parent, (exp.From, exp.Join)) and is_match_select(inner) and not is_match_select(expression.parent.parent)
        and not {key for key, value in expression.args.items() if value not in (None, [], False)} - {"this", "alias"}
    ):
        alias = generator.sql(expression, "alias")
        alias = f" AS {alias}" if alias else ""
        # A clause that reads another clause keeps the parentheses around it, which the check above leaves alone.
        return f"{generator.sql(_from_of(inner).this)} {generator.sql(inner, 'match').strip()}{alias}"
    return fallback(generator, expression)



def text_has_clause(sql: str) -> bool:
    """Whether the text holds a ``MATCH_RECOGNIZE (`` table operator (found in the tokens, so a string or comment does not count).

    Text that does not tokenize counts as holding one: the callers use this to decline, and declining is the safe answer.
    """

    if "match_recognize" not in sql.lower():
        return False
    try:
        import sqlglot

        tokens = sqlglot.tokenize(sql, read="bigquery")
    except Exception:  # noqa: BLE001
        return True
    return any(_is_keyword(token) and following.token_type == TokenType.L_PAREN for token, following in zip(tokens, tokens[1:]))
