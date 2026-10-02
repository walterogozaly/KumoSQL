"""BigQuery syntax that sqlglot's BigQuery parser does not read, added once for every parse in this package.

``FROM dataset.fn(TABLE dataset.input, option => value)`` passes a whole table to a table-valued function. sqlglot stops at
``TABLE`` ("Expecting )"), so the model was reported unparseable and everything it read was lost. Here the argument becomes a
:class:`TableArgument` holding the table, so the table is read like any other and the SQL prints back unchanged.
"""

from __future__ import annotations

from sqlglot import exp
from sqlglot.dialects.bigquery import BigQuery
from sqlglot.generator import Generator
from sqlglot.tokens import TokenType


class TableArgument(exp.Expression):
    """``TABLE name`` (or ``TABLE (SELECT ...)``) as an argument of a table-valued function."""

    arg_types = {"this": True}


def _table_argument_sql(self: Generator, expression: TableArgument) -> str:
    return f"TABLE {self.sql(expression, 'this')}"


_installed = False


def install() -> None:
    global _installed
    if _installed:
        return
    _installed = True
    previous = BigQuery.Parser._parse_lambda

    def _parse_lambda(self, *args, **kwargs):
        if self._curr is not None and self._curr.token_type == TokenType.TABLE and self._next is not None and self._next.token_type not in (
            TokenType.COMMA,
            TokenType.R_PAREN,
        ):
            index = self._index
            self._advance()
            table = self._parse_table(schema=False)
            if table is not None:
                return TableArgument(this=table)
            self._retreat(index)
        return previous(self, *args, **kwargs)

    BigQuery.Parser._parse_lambda = _parse_lambda
    Generator.tableargument_sql = _table_argument_sql
    # Releases differ in how a generator finds its handler (a method named after the class, a per-class table, a cache of both),
    # so every generator that exists gets the handler in its own table and any cache is dropped.
    pending = [Generator]
    while pending:
        generator = pending.pop()
        generator.TRANSFORMS[TableArgument] = _table_argument_sql
        pending.extend(generator.__subclasses__())
    from sqlglot import generator as generator_module

    getattr(generator_module, "_DISPATCH_CACHE", {}).clear()


install()
