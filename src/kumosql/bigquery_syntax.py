"""BigQuery syntax that sqlglot's BigQuery parser does not read, added once for every parse in this package.

``DROP TABLE FUNCTION`` is read as dropping a function of kind ``TABLE FUNCTION``.

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


def _generate(self: Generator, expression: exp.Anonymous) -> str:
    if expression.name == TABLE_ARGUMENT and len(expression.expressions) == 1:
        return f"TABLE {self.sql(expression.expressions[0])}"
    return self.anonymous_sql(expression)


_installed = False


def _in_native_call(before: list) -> bool:
    """Inside ``ML.fn(...)`` or ``AI.fn(...)``, which sqlglot already reads with its own ``TABLE`` handling."""

    depth = 0
    for index in range(len(before) - 1, -1, -1):
        kind = before[index].token_type
        if kind == TokenType.R_PAREN:
            depth += 1
        elif kind == TokenType.L_PAREN:
            if depth == 0:
                return index >= 3 and before[index - 2].token_type == TokenType.DOT and before[index - 3].text.upper() in ("ML", "AI")
            depth -= 1
    return False


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
        except ParseError:
            tokens = self.tokenize(sql)
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
