"""Skips for the sqlglot 26.0.0 floor of the supported matrix (26.0.0, 30.20, 30.21).

sqlglot 26 cannot parse a few shapes the tests exercise (outer ``BY NAME``, pipe syntax, an empty grouping set
inside ``GROUPING SETS``, a parenthesised set operation with its own ``ORDER BY ... LIMIT``). Where the test checks
rows or a printed form it cannot build without that parse, it skips there and says why. Only releases before 27
skip: a parse that fails on a newer sqlglot is a real failure.
"""

import pytest
import sqlglot

OLD_SQLGLOT = int(sqlglot.__version__.split(".")[0]) < 27


def skip_if_unparseable(*queries: str, dialect: str = "bigquery") -> None:
    """Skip the running test when sqlglot 26 cannot parse one of ``queries`` (a no-op on newer releases)."""

    if not OLD_SQLGLOT:
        return
    for query in queries:
        try:
            sqlglot.parse_one(query, read=dialect)
        except sqlglot.errors.ParseError:
            pytest.skip(f"sqlglot {sqlglot.__version__} cannot parse: {query[:70]}")
