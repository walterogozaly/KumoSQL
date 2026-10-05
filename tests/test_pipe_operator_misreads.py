"""Pipe operators that sqlglot read as a different query are written plainly or refused, never misread.

sqlglot folds ``FROM t |> a |> b`` into one ``SELECT``; after a ``|> LIMIT`` a later ``|> WHERE`` / ``|> ORDER BY`` / ``|> JOIN``
used to be read as acting before the limit, ``|> PIVOT`` and ``|> SELECT DISTINCT`` were dropped, ``SELECT AS STRUCT`` lost its
``AS STRUCT``, ``GROUP AND ORDER BY`` lost its ordering and ``GROUP BY ROLLUP (x)`` became a column called ``ROLLUP``.
"""

from __future__ import annotations

import pytest
import sqlglot
from sqlglot.errors import ParseError
from sqlglot.tokens import TokenType

import kumosql  # noqa: F401  (installs the BigQuery syntax additions)
from kumosql.ast_utils import parse_statements

pytestmark = pytest.mark.skipif(not hasattr(TokenType, "PIPE_GT"), reason="this sqlglot has no pipe syntax")

SRC = "FROM UNNEST([1, 2, 3]) AS x"


def read(sql: str) -> str:
    (statement,) = parse_statements(sql)
    return statement.sql("bigquery")


@pytest.mark.parametrize(
    "sql",
    [
        f"{SRC} |> LIMIT 1 |> ORDER BY x",
        f"{SRC} |> LIMIT 1 |> WHERE x > 1",
        f"{SRC} |> LIMIT 2 |> JOIN UNNEST([1]) AS y ON x = y",
        f"{SRC} |> LIMIT 2 |> DISTINCT",
        f"{SRC} |> LIMIT 5 |> LIMIT 2",
        f"{SRC} |> ORDER BY x |> LIMIT 1 |> WHERE x > 1",
        f"{SRC} |> DISTINCT |> JOIN UNNEST([1]) AS y ON x = y",
        f"{SRC} |> WHERE x > 1 |> RIGHT JOIN UNNEST([1]) AS y ON x = y",
        f"{SRC} |> WHERE x > 1 |> FULL OUTER JOIN UNNEST([1]) AS y ON x = y",
        f"{SRC} |> SELECT AS STRUCT x",
        f"{SRC} |> SELECT AS VALUE STRUCT(x)",
        f"{SRC} |> SELECT SUM(x) OVER w AS s WINDOW w AS (ORDER BY x)",
        f"{SRC} |> AGGREGATE COUNT(*) AS c GROUP BY ROLLUP(x)",
        f"{SRC} |> AGGREGATE COUNT(*) AS c GROUP BY CUBE(x)",
        f"{SRC} |> AGGREGATE COUNT(*) AS c GROUP BY GROUPING SETS (x, ())",
        f"{SRC} |> AGGREGATE COUNT(*) AS c GROUP AND ORDER BY x NULLS LAST",
        f"{SRC} |> TABLESAMPLE SYSTEM (50 PERCENT) |> WHERE x > 1",
        f"{SRC} |> WHERE x > 1 |> TABLESAMPLE SYSTEM (50 PERCENT)",
        f"{SRC} |> SELECT x |> TABLESAMPLE SYSTEM (50 PERCENT)",
        f"SELECT * FROM ({SRC} |> LIMIT 1 |> WHERE x > 1)",
        f"{SRC} |> UNION ALL ({SRC} |> LIMIT 1 |> ORDER BY x)",
    ],
)
def test_what_a_single_select_would_misread_is_refused(sql):
    with pytest.raises(ParseError):
        parse_statements(sql)
    with pytest.raises(ParseError):
        sqlglot.parse_one(sql, read="bigquery")


@pytest.mark.parametrize(
    "sql, expected",
    [
        (f"{SRC} |> SELECT DISTINCT x, x + 1 AS y", f"WITH __tmp1 AS (SELECT x, x + 1 AS y FROM UNNEST([1, 2, 3]) AS x) SELECT DISTINCT * FROM __tmp1"),
        (f"{SRC} |> SELECT DISTINCT x |> WHERE x > 1", f"WITH __tmp1 AS (SELECT x FROM UNNEST([1, 2, 3]) AS x) SELECT DISTINCT * FROM __tmp1 WHERE x > 1"),
        (f"{SRC} |> WINDOW SUM(x) OVER () AS s", f"WITH __tmp1 AS (SELECT *, SUM(x) OVER () AS s FROM UNNEST([1, 2, 3]) AS x) SELECT * FROM __tmp1"),
        (
            f"{SRC} |> AGGREGATE COUNT(*) AS c GROUP AND ORDER BY x, x + 1 AS y DESC",
            "WITH __tmp1 AS (SELECT x, x + 1 AS y, COUNT(*) AS c FROM UNNEST([1, 2, 3]) AS x GROUP BY x, y ORDER BY x ASC, y DESC) SELECT * FROM __tmp1",
        ),
        (
            f"{SRC} |> AGGREGATE COUNT(*) AS c GROUP AND ORDER BY x AS g",
            "WITH __tmp1 AS (SELECT x AS g, COUNT(*) AS c FROM UNNEST([1, 2, 3]) AS x GROUP BY g ORDER BY g ASC) SELECT * FROM __tmp1",
        ),
        (
            f"{SRC} |> AGGREGATE COUNT(*) AS c GROUP AND ORDER BY f(x, 1) DESC NULLS LAST",
            "WITH __tmp1 AS (SELECT f(x, 1), COUNT(*) AS c FROM UNNEST([1, 2, 3]) AS x GROUP BY f(x, 1) ORDER BY f(x, 1) DESC) SELECT * FROM __tmp1",
        ),
    ],
)
def test_forms_with_a_plain_spelling_are_read_through_it(sql, expected):
    assert read(sql) == expected


@pytest.mark.parametrize(
    "sql",
    [
        f"{SRC} |> ORDER BY x |> LIMIT 1",
        f"{SRC} |> WHERE x > 1 |> ORDER BY x |> LIMIT 2 OFFSET 1",
        f"{SRC} |> LIMIT 2 |> AS t |> WHERE t.x > 1",
        f"{SRC} |> LIMIT 2 |> SELECT x + 1 AS y |> WHERE y > 1",
        f"{SRC} |> LIMIT 2 |> AGGREGATE COUNT(*) AS c",
        f"{SRC} |> LIMIT 2 |> EXTEND x * 2 AS y |> ORDER BY y",
        f"{SRC} |> WHERE x > 1 |> LEFT JOIN UNNEST([1]) AS y ON x = y",
        f"{SRC} |> JOIN UNNEST([1]) AS y ON x = y |> WHERE y > 0 |> LIMIT 1",
        f"{SRC} |> DISTINCT |> WHERE x > 1 |> LIMIT 1",
        f"{SRC} |> TABLESAMPLE SYSTEM (50 PERCENT) |> AGGREGATE COUNT(*)",
        f"{SRC} |> TABLESAMPLE SYSTEM (50 PERCENT)",
        f"SELECT * FROM ({SRC} |> LIMIT 1) WHERE x > 1",
        f"{SRC} |> UNPIVOT(v FOR k IN (x))",
        f"{SRC} |> PIVOT(COUNT(*) FOR x IN (1, 2))",
        f"{SRC} |> SELECT x, SUM(x) OVER (ORDER BY x) AS s |> WHERE s > 1",
        f"{SRC} |> UNION ALL ({SRC} |> LIMIT 1), ({SRC} |> WHERE x > 1)",
    ],
)
def test_orders_a_select_can_say_are_still_read(sql):
    assert parse_statements(sql)


def test_a_limit_read_before_a_later_order_is_not_merged_into_it():
    # FROM t |> LIMIT 1 |> ORDER BY x sorts the one row left; "ORDER BY x LIMIT 1" would pick the smallest x of all.
    for sql in (f"{SRC} |> LIMIT 1 |> ORDER BY x", f"{SRC} |> LIMIT 1 |> WHERE x > 1"):
        with pytest.raises(ParseError):
            parse_statements(sql)
    assert read(f"{SRC} |> ORDER BY x |> LIMIT 1") == "SELECT * FROM UNNEST([1, 2, 3]) AS x ORDER BY x LIMIT 1"


@pytest.mark.parametrize(
    "sql, expected",
    [
        (
            "FROM (SELECT 'a' AS k, 1 AS v) |> PIVOT(SUM(v) FOR k IN ('a'))",
            "SELECT * FROM (SELECT * FROM (SELECT 'a' AS k, 1 AS v)) PIVOT(SUM(v) FOR k IN ('a'))",
        ),
        (
            "FROM (SELECT 1 AS a, 2 AS b) |> WHERE a > 0 |> UNPIVOT(v FOR k IN (a, b)) AS u |> WHERE v > 1 |> LIMIT 1",
            "SELECT * FROM (SELECT * FROM (SELECT * FROM (SELECT * FROM (SELECT 1 AS a, 2 AS b) WHERE a > 0)) UNPIVOT(v FOR k IN (a, b)) AS u WHERE v > 1 LIMIT 1)",
        ),
        (
            "WITH w AS (SELECT 1 AS a, 2 AS b) FROM w |> UNPIVOT(v FOR k IN (a, b)) |> UNPIVOT(z FOR y IN (v))",
            "SELECT * FROM (SELECT * FROM (WITH w AS (SELECT 1 AS a, 2 AS b) SELECT * FROM w) UNPIVOT(v FOR k IN (a, b))) UNPIVOT(z FOR y IN (v))",
        ),
    ],
)
def test_a_pipe_pivot_is_the_table_pivot_of_the_query_before_it(sql, expected):
    # a filter before |> UNPIVOT acts on the columns being unpivoted, so it must stay inside, before the unpivot
    assert read(sql) == expected


def test_a_refusal_is_a_parse_error_naming_the_operator():
    with pytest.raises(ParseError, match="AS STRUCT"):
        parse_statements(f"{SRC} |> SELECT AS STRUCT x")
    with pytest.raises(ParseError, match="LIMIT"):
        parse_statements(f"{SRC} |> LIMIT 1 |> WHERE x > 1")


def test_standard_syntax_is_untouched():
    assert read("SELECT DISTINCT x FROM t WHERE x > 1 ORDER BY x LIMIT 2") == "SELECT DISTINCT x FROM t WHERE x > 1 ORDER BY x LIMIT 2"
    assert read("SELECT AS STRUCT 1 AS a") == "SELECT AS STRUCT 1 AS a"
    assert read("SELECT a FROM t WHERE a IS DISTINCT FROM b") == "SELECT a FROM t WHERE a IS DISTINCT FROM b"
