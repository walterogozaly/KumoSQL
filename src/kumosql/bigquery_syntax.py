"""BigQuery syntax that sqlglot's BigQuery parser does not read, added once for every parse in this package.

``DROP TABLE FUNCTION`` is read as dropping a function of kind ``TABLE FUNCTION``. Pipe ``SET`` and ``DROP`` are read as the
``SELECT * REPLACE`` and ``SELECT * EXCEPT`` they stand for. ``GRAPH_TABLE(...)`` and the ``MODEL m`` argument of an ``ML.``
function sqlglot does not know are kept as their own text.

``FROM dataset.fn(TABLE dataset.input, option => value)`` passes a whole table to a table-valued function. sqlglot stops at
``TABLE`` ("Expecting )"), so the model was reported unparseable and everything it read was lost. Here the argument becomes a
:data:`TABLE_ARGUMENT` call holding the table, so the table is read like any other and the SQL prints back unchanged.

It works the same on pure and compiled sqlglot: a compiled build ignores parser methods assigned after the fact and refuses
subclasses of its expressions, so SQL sqlglot rejects is parsed again with the dialect's tokens rewritten and the marker call is resolved after.
"""

from __future__ import annotations

from sqlglot import exp
from sqlglot.dialects.bigquery import BigQuery
from sqlglot.errors import ParseError
from sqlglot.generator import Generator
from sqlglot.tokens import Token, TokenType


TABLE_ARGUMENT = "__KUMO_TABLE_ARGUMENT__"


VERBATIM = "__KUMO_VERBATIM__"
# Calls whose arguments are a language of their own, kept as their text: ``GRAPH_TABLE(graph MATCH ... RETURN ...)``.
_VERBATIM_CALLS = ("GRAPH_TABLE",)


def _generate(self: Generator, expression: exp.Anonymous) -> str:
    if expression.name == TABLE_ARGUMENT and len(expression.expressions) == 1:
        return f"TABLE {self.sql(expression.expressions[0])}"
    if expression.name == VERBATIM and len(expression.expressions) == 1 and expression.expressions[0].is_string:
        return expression.expressions[0].this
    return self.anonymous_sql(expression)


_installed = False


def _open_call(before: list) -> int | None:
    """The index of the ``(`` of the innermost call still open at the end of ``before``."""

    depth = 0
    for index in range(len(before) - 1, -1, -1):
        kind = before[index].token_type
        if kind == TokenType.R_PAREN:
            depth += 1
        elif kind == TokenType.L_PAREN:
            if depth == 0:
                return index
            depth -= 1
    return None


def _ml_call(before: list, index: int | None) -> str | None:
    """The function name when the ``(`` at ``index`` opens ``ML.fn(`` or ``AI.fn(``."""

    if index is None or index < 3 or before[index - 2].token_type != TokenType.DOT:
        return None
    if before[index - 3].text.upper() not in ("ML", "AI"):
        return None
    return before[index - 1].text.upper()


def _in_native_call(before: list) -> bool:
    """Inside an ``ML.fn(...)`` or ``AI.fn(...)`` that sqlglot reads itself (``ML.PREDICT``), with its own ``TABLE`` handling."""

    name = _ml_call(before, _open_call(before))
    return name is not None and name in BigQuery.Parser.FUNCTION_PARSERS


def _mark_table_arguments(tokens: list) -> list:
    """``TABLE name`` as an argument becomes ``__KUMO_TABLE_ARGUMENT__(name)``, which every sqlglot build parses as a call."""

    out: list = []
    i = 0
    count = len(tokens)
    while i < count:
        token = tokens[i]
        follows_open = bool(out) and out[-1].token_type in (TokenType.L_PAREN, TokenType.COMMA)
        if follows_open and _in_native_call(out):
            follows_open = False
        if (
            token.token_type == TokenType.TABLE
            and follows_open
            and i + 1 < count
            and tokens[i + 1].token_type not in (TokenType.COMMA, TokenType.R_PAREN)
        ):
            depth = 0
            end = i + 1
            while end < count:
                kind = tokens[end].token_type
                if kind == TokenType.L_PAREN:
                    depth += 1
                elif kind == TokenType.R_PAREN:
                    if depth == 0:
                        break
                    depth -= 1
                elif kind == TokenType.COMMA and depth == 0:
                    break
                end += 1
            if end < count:
                def made(kind, text, source=token):
                    return Token(kind, text, line=source.line, col=source.col, start=source.start, end=source.end)

                out.append(made(TokenType.VAR, TABLE_ARGUMENT))
                out.append(made(TokenType.L_PAREN, "("))
                out.extend(tokens[i + 1 : end])
                out.append(made(TokenType.R_PAREN, ")"))
                i = end
                continue
        out.append(token)
        i += 1
    return out


def _merge_table_function_kind(tokens: list) -> list:
    """``DROP TABLE FUNCTION`` becomes ``DROP`` and one ``FUNCTION`` token spelled ``TABLE FUNCTION``: sqlglot takes the kind
    of a ``DROP`` from that token's text, so it parses as a dropped function of kind ``TABLE FUNCTION`` and prints back."""

    out: list = []
    i = 0
    while i < len(tokens):
        token = tokens[i]
        if (
            token.token_type == TokenType.TABLE
            and out and out[-1].token_type == TokenType.DROP
            and i + 1 < len(tokens) and tokens[i + 1].token_type == TokenType.FUNCTION
        ):
            end = tokens[i + 1]
            out.append(Token(TokenType.FUNCTION, "TABLE FUNCTION", line=token.line, col=token.col, start=token.start, end=end.end))
            i += 2
            continue
        out.append(token)
        i += 1
    return out


# ``RENAME a AS b`` is left out on purpose: it keeps ``b`` in ``a``'s place, which no ``SELECT`` can say without knowing the
# columns, and a translation that moved the column would let a prover equate queries whose columns are in different orders.
_PIPE_OPERATORS = (TokenType.SET, TokenType.DROP)
_PIPE = getattr(TokenType, "PIPE_GT", None)  # sqlglot 26.0.0 has no pipe syntax at all


def _pipe_items(tokens: list, start: int) -> tuple[list[list], int]:
    """The comma-separated items of a pipe operator whose arguments start at ``start``, and the index after them."""

    items: list[list] = [[]]
    depth = 0
    index = start
    while index < len(tokens):
        kind = tokens[index].token_type
        if depth == 0 and kind in (_PIPE, TokenType.SEMICOLON):
            break
        if kind in (TokenType.L_PAREN, TokenType.L_BRACKET):
            depth += 1
        elif kind in (TokenType.R_PAREN, TokenType.R_BRACKET):
            if depth == 0:
                break
            depth -= 1
        if depth == 0 and kind == TokenType.COMMA:
            items.append([])
        else:
            items[-1].append(tokens[index])
        index += 1
    return [item for item in items if item], index


def _pipe_operator_text(sql: str, operator, items: list[list]) -> str | None:
    """``|> SET c = e`` as ``|> SELECT * REPLACE (e AS c)`` and ``|> DROP c`` as ``|> SELECT * EXCEPT (c)``, which BigQuery
    defines them to mean, or None when an item is not plain."""

    def text(tokens: list) -> str:
        return sql[tokens[0].start : tokens[-1].end + 1]

    if not items:
        return None
    if operator == TokenType.DROP:
        if any(len(item) != 1 for item in items):
            return None
        return f"|> SELECT * EXCEPT ({', '.join(text(item) for item in items)})"
    parts = []
    for item in items:
        if len(item) < 3 or item[1].token_type != TokenType.EQ:
            return None
        parts.append(f"{text(item[2:])} AS {text(item[:1])}")
    return f"|> SELECT * REPLACE ({', '.join(parts)})"


def _rewrite_pipe_operators(sql: str, tokens: list) -> str:
    """Pipe ``SET`` and ``DROP``, which sqlglot does not read, written as the pipe ``SELECT`` that means the same."""

    pieces: list[str] = []
    position = 0
    index = 0
    while index < len(tokens):
        token = tokens[index]
        if (
            _PIPE is not None
            and token.token_type == _PIPE
            and index + 1 < len(tokens)
            and tokens[index + 1].token_type in _PIPE_OPERATORS
        ):
            items, after = _pipe_items(tokens, index + 2)
            replacement = _pipe_operator_text(sql, tokens[index + 1].token_type, items)
            if replacement is not None:
                end = tokens[after - 1].end + 1
                pieces += [sql[position : token.start], replacement]
                position = end
                index = after
                continue
        index += 1
    return "".join(pieces) + sql[position:] if pieces else sql


def _rewrite_verbatim_calls(sql: str, tokens: list) -> str:
    """``GRAPH_TABLE(...)`` becomes ``__KUMO_VERBATIM__('<its text>')``, which prints back as the text it holds.

    The rest of the query is read as usual. The call is an unknown function of its own text, so it reads no table sqlglot
    can see and a prover can only equate it with the same text.
    """

    pieces: list[str] = []
    position = 0
    index = 0
    while index + 1 < len(tokens):
        token = tokens[index]
        if token.text.upper() in _VERBATIM_CALLS and tokens[index + 1].token_type == TokenType.L_PAREN:
            depth = 0
            end = index + 1
            while end < len(tokens):
                kind = tokens[end].token_type
                depth += kind == TokenType.L_PAREN
                depth -= kind == TokenType.R_PAREN
                if depth == 0:
                    break
                end += 1
            if end < len(tokens):
                text = sql[token.start : tokens[end].end + 1]
                pieces += [sql[position : token.start], f"{VERBATIM}({_quoted(text)})"]
                position = tokens[end].end + 1
                index = end + 1
                continue
        index += 1
    return "".join(pieces) + sql[position:] if pieces else sql


def _rewrite_model_arguments(sql: str, tokens: list) -> str:
    """``MODEL name`` as an argument of an ``ML.fn`` sqlglot does not read (``ML.EVALUATE``, ``ML.DETECT_ANOMALIES``) becomes
    ``__KUMO_VERBATIM__('MODEL name')``: it prints back as written and, a model not being a table, is no read."""

    pieces: list[str] = []
    position = 0
    for index, token in enumerate(tokens):
        if token.text.upper() != "MODEL" or index == 0 or index + 1 >= len(tokens):
            continue
        if tokens[index - 1].token_type not in (TokenType.L_PAREN, TokenType.COMMA):
            continue
        name = _ml_call(tokens, _open_call(tokens[:index]))
        if name is None or name in BigQuery.Parser.FUNCTION_PARSERS:
            continue
        end = index + 1
        while end + 1 < len(tokens) and tokens[end + 1].token_type not in (TokenType.COMMA, TokenType.R_PAREN):
            end += 1
        text = sql[token.start : tokens[end].end + 1]
        pieces += [sql[position : token.start], f"{VERBATIM}({_quoted(text)})"]
        position = tokens[end].end + 1
    return "".join(pieces) + sql[position:] if pieces else sql


def _quoted(text: str) -> str:
    """``text`` as a BigQuery string literal that reads back as exactly ``text``."""

    escaped = text.replace("\\", "\\\\").replace("'", "\\'").replace("\n", "\\n").replace("\r", "\\r")
    return f"'{escaped}'"


def _table_argument(call: exp.Anonymous) -> exp.Expression:
    arguments = call.expressions
    inner = arguments[0] if len(arguments) == 1 else None
    if isinstance(inner, (exp.Column, exp.Dot)):
        try:
            table = exp.to_table(inner.sql("bigquery"), dialect="bigquery")
        except Exception:
            return call
        return exp.Anonymous(this=TABLE_ARGUMENT, expressions=[table])
    return call


def _resolve_table_arguments(trees):
    for tree in trees:
        if tree is None:
            continue
        for call in list(tree.find_all(exp.Anonymous)):
            if call.name == TABLE_ARGUMENT:
                replacement = _table_argument(call)
                if call.parent is None:
                    tree = replacement
                elif replacement is not call:
                    call.replace(replacement)
        yield tree


def install() -> None:
    global _installed
    if _installed:
        return
    _installed = True
    # The parser is not touched: a compiled sqlglot (sqlglotc) dispatches parser methods through a table built when the class
    # is defined, ignores a method assigned afterwards and refuses a subclass. SQL it rejects is parsed again with the
    # tokens rewritten, and the marker call is turned into a table argument after, which works the same on every build.
    parse = BigQuery.parse

    def parse_with_table_arguments(self, sql, **opts):
        # Whatever sqlglot reads on its own is left alone; only SQL it rejects is retried with the table arguments marked.
        try:
            return parse(self, sql, **opts)
        except ParseError as error:
            tokens = self.tokenize(sql)
            rewritten = sql
            for rewrite in (_rewrite_verbatim_calls, _rewrite_model_arguments, _rewrite_pipe_operators):
                rewritten = rewrite(rewritten, tokens if rewritten == sql else self.tokenize(rewritten))
            if rewritten != sql:
                try:
                    return parse_with_table_arguments(self, rewritten, **opts)
                except ParseError:
                    raise error from None
            marked = _mark_table_arguments(_merge_table_function_kind(tokens))
            if len(marked) == len(tokens) and all(a is b for a, b in zip(marked, tokens)):
                raise
            return list(_resolve_table_arguments(self.parser(**opts).parse(marked, sql)))

    BigQuery.parse = parse_with_table_arguments
    # Releases differ in how a generator finds its handler (a method named after the class, a per-class table, a cache of both),
    # so every generator that exists gets the handler in its own table and any cache is dropped.
    pending = [Generator]
    while pending:
        generator = pending.pop()
        generator.TRANSFORMS[exp.Anonymous] = _generate
        pending.extend(generator.__subclasses__())
    from sqlglot import generator as generator_module

    getattr(generator_module, "_DISPATCH_CACHE", {}).clear()

install()
