"""GoogleSQL pipe syntax in the type checker (src/kumosql/googlesql_pipe_types.py).

Each case is a pipe query over a small catalog and the output columns GoogleSQL gives it. An operator the module does not
model must leave the type unknown, never a different one.
"""

import pytest

from kumosql.googlesql_types import Catalog, infer

CATALOG = Catalog.from_types(
    {
        "t": {"a": "INT64", "b": "STRING", "c": "FLOAT64", "arr": "ARRAY<INT64>", "s": "STRUCT<x INT64, y STRING>"},
        "u": {"a": "INT64", "d": "DATE"},
        "v": {"k": "INT64", "name": "STRING"},
    }
)


def columns(sql: str):
    typed = infer(sql, CATALOG)
    if typed.columns is None:
        return None
    return [(c.name, c.type.sql() if c.type is not None else None) for c in typed.columns]


@pytest.mark.parametrize(
    "sql, expected",
    [
        ("FROM t |> WHERE a > 1 |> SELECT a, b, c * 2 AS c2", [("a", "INT64"), ("b", "STRING"), ("c2", "FLOAT64")]),
        ("SELECT a, b FROM t |> WHERE a > 1 |> ORDER BY a |> LIMIT 3 OFFSET 1", [("a", "INT64"), ("b", "STRING")]),
        ("FROM t |> SELECT * EXCEPT (arr, s)", [("a", "INT64"), ("b", "STRING"), ("c", "FLOAT64")]),
        ("FROM t |> SELECT s.*", [("x", "INT64"), ("y", "STRING")]),
        ("FROM t AS tt |> WHERE tt.a > 0 |> SELECT tt.a", [("a", "INT64")]),
        ("FROM t |> SELECT a, b |> SELECT DISTINCT b", [("b", "STRING")]),
        ("FROM t |> SELECT COUNT(*), a + 1.5", [(None, "INT64"), (None, "FLOAT64")]),
        ("FROM t |> SELECT a, ROW_NUMBER() OVER w WINDOW w AS (ORDER BY a)", [("a", "INT64"), (None, "INT64")]),
        ("SELECT 1 AS x, NULL AS n, [] AS e |> SELECT *", [("x", "INT64"), ("n", "INT64"), ("e", "ARRAY<INT64>")]),
    ],
)
def test_select_where_order_limit(sql, expected):
    assert columns(sql) == expected


@pytest.mark.parametrize(
    "sql, expected",
    [
        ("FROM t |> SELECT a, b |> EXTEND a + 1 AS a1, UPPER(b)", [("a", "INT64"), ("b", "STRING"), ("a1", "INT64"), (None, "STRING")]),
        ("FROM t |> SELECT a, b |> WINDOW SUM(a) OVER (PARTITION BY b)", [("a", "INT64"), ("b", "STRING"), (None, "INT64")]),
        ("FROM t |> SELECT a, b |> SET a = a * 1.5, b = 'x'", [("a", "FLOAT64"), ("b", "STRING")]),
        ("FROM t |> SELECT a, b, c |> DROP b", [("a", "INT64"), ("c", "FLOAT64")]),
        ("FROM t |> SELECT a, b |> RENAME a AS b, b AS a", [("b", "INT64"), ("a", "STRING")]),
        ("FROM t |> SELECT a, b |> AS q |> SELECT q, q.a", [("q", "STRUCT<a INT64, b STRING>"), ("a", "INT64")]),
    ],
)
def test_extend_set_drop_rename_as(sql, expected):
    assert columns(sql) == expected


def test_rename_does_not_hide_the_operators_after_it():
    # sqlglot reads everything after RENAME as one string; the operators after it are still operators
    assert columns("FROM t |> SELECT a, b |> RENAME a AS x |> WHERE x > 1 |> SELECT b") == [("b", "STRING")]


@pytest.mark.parametrize(
    "sql, expected",
    [
        ("FROM t |> AGGREGATE COUNT(*) AS n, SUM(c) AS sc GROUP BY b", [("b", "STRING"), ("n", "INT64"), ("sc", "FLOAT64")]),
        ("FROM t |> AGGREGATE COUNT(*), MIN(a)", [(None, "INT64"), (None, "INT64")]),
        ("FROM t |> AGGREGATE COUNT(*) DESC GROUP BY b DESC, s.x", [("b", "STRING"), ("x", "INT64"), (None, "INT64")]),
        ("FROM t |> AGGREGATE MAX(a) GROUP AND ORDER BY b", [("b", "STRING"), (None, "INT64")]),
        ("FROM t |> AGGREGATE GROUP BY b, a + 1 AS a1", [("b", "STRING"), ("a1", "INT64")]),
        ("FROM t |> AGGREGATE SUM(a) + a AS v GROUP BY a |> WHERE v > 1", [("a", "INT64"), ("v", "INT64")]),
    ],
)
def test_aggregate(sql, expected):
    assert columns(sql) == expected


@pytest.mark.parametrize(
    "sql",
    [
        "FROM t |> AGGREGATE COUNT(*) GROUP BY 1",  # which expression 1 is, is not settled
        "FROM t |> AGGREGATE COUNT(*) GROUP BY ROLLUP(a, b)",
        "FROM t |> AGGREGATE",
    ],
)
def test_aggregate_forms_left_unknown(sql):
    assert columns(sql) is None


@pytest.mark.parametrize(
    "sql, expected",
    [
        ("FROM t |> JOIN u USING (a) |> SELECT *", None),  # checked below
        ("FROM t |> JOIN u ON t.a = u.a |> SELECT t.a, u.d", [("a", "INT64"), ("d", "DATE")]),
        ("FROM t |> LEFT JOIN u ON t.a = u.a |> SELECT a", [("a", None)]),  # ambiguous in GoogleSQL
        ("FROM t |> CROSS JOIN v |> SELECT t.a, k, name", [("a", "INT64"), ("k", "INT64"), ("name", "STRING")]),
        ("FROM t |> JOIN UNNEST(arr) AS e |> SELECT e, a", [("e", "INT64"), ("a", "INT64")]),
        ("FROM t |> SELECT a, b |> AS q |> JOIN v ON q.b = v.name |> SELECT q.a, v.k", [("a", "INT64"), ("k", "INT64")]),
    ],
)
def test_join(sql, expected):
    if expected is None:
        got = columns(sql)
        assert [c for c, _ in got] == ["a", "b", "c", "arr", "s", "d"]
    else:
        assert columns(sql) == expected


@pytest.mark.parametrize(
    "sql, expected",
    [
        ("FROM t |> SELECT a |> UNION ALL (FROM u |> SELECT a), (SELECT 7)", [("a", "INT64")]),
        ("FROM t |> SELECT a, b |> UNION DISTINCT BY NAME (SELECT 'z' AS b, 1 AS a)", [("a", "INT64"), ("b", "STRING")]),
        ("FROM t |> SELECT a |> EXCEPT DISTINCT (SELECT a FROM u) |> EXTEND a + 1", [("a", "INT64"), (None, "INT64")]),
        ("FROM t |> SELECT a |> INTERSECT ALL (SELECT 1)", [("a", "INT64")]),
        ("(FROM t |> SELECT a) UNION ALL (FROM u |> SELECT a)", [("a", "INT64")]),
        ("(FROM t |> SELECT a) UNION ALL (SELECT 'x')", [("a", None)]),  # no common type: unknown
    ],
)
def test_set_operations(sql, expected):
    assert columns(sql) == expected


def test_with_names_queries_for_the_operators_after_it():
    assert columns("FROM t |> SELECT a |> WITH w AS (FROM u |> SELECT d) |> CROSS JOIN w") == [("a", "INT64"), ("d", "DATE")]
    assert columns("WITH w AS (FROM t |> SELECT a) FROM w |> EXTEND a + 1 AS a1") == [("a", "INT64"), ("a1", "INT64")]


def test_pivot_and_unpivot_use_the_pivot_rules():
    assert columns("FROM t |> SELECT a, b |> PIVOT (COUNT(*) FOR b IN ('p', 'q'))") == [
        ("a", "INT64"), ("p", "INT64"), ("q", "INT64")]
    assert columns("FROM t |> SELECT a, a AS a2 |> UNPIVOT (v FOR col IN (a, a2))") == [("v", "INT64"), ("col", "STRING")]


def test_describe_does_not_need_its_input():
    assert columns("FROM nosuchtable |> WHERE x > 1 |> DESCRIBE") == [("Describe", "STRING")]
    assert columns("FROM t |> DESCRIBE |> SELECT Describe, LENGTH(Describe)") == [("Describe", "STRING"), (None, "INT64")]
    assert columns("FROM nosuchtable |> WHERE x > 1 |> SELECT *") is None


def test_nested_chains():
    assert columns("SELECT (FROM u |> AGGREGATE MAX(d)) AS m") == [("m", "DATE")]
    assert columns("SELECT ARRAY(FROM t |> SELECT b) AS bs") == [("bs", "ARRAY<STRING>")]
    assert columns("SELECT * FROM (FROM t |> SELECT a, b) AS q") == [("a", "INT64"), ("b", "STRING")]
    assert columns("FROM t |> EXTEND (FROM u |> WHERE u.a = t.a |> AGGREGATE COUNT(*)) AS cnt |> SELECT cnt") == [
        ("cnt", "INT64")]
    assert columns("FROM t |> EXTEND ARRAY(FROM UNNEST(arr) AS e |> SELECT e * 1.5) AS big |> SELECT big") == [
        ("big", "ARRAY<FLOAT64>")]


def test_unnest_value_tables():
    assert columns("FROM UNNEST([1, 2, 3]) AS x |> EXTEND x * 2 AS y |> SELECT x, y") == [("x", "INT64"), ("y", "INT64")]
    assert columns("FROM UNNEST([1, 2, 3]) AS x |> AGGREGATE SUM(x) AS s, ARRAY_AGG(x) AS xs") == [
        ("s", "INT64"), ("xs", "ARRAY<INT64>")]


def test_match_recognize():
    sql = """FROM t |> MATCH_RECOGNIZE(
      PARTITION BY b
      ORDER BY a
      MEASURES MAX(up.a) AS hi, CLASSIFIER() AS who, FIRST(c) AS first_c, ARRAY_AGG(MATCH_ROW_NUMBER()) AS rows,
               MAX(a + AVG(c) GROUP BY b) AS multi
      PATTERN (up down*)
      DEFINE up AS a > 0, down AS a < 0
    )"""
    assert columns(sql) == [
        ("b", "STRING"), ("hi", "INT64"), ("who", "STRING"), ("first_c", "FLOAT64"), ("rows", "ARRAY<INT64>"),
        ("multi", "FLOAT64")]
    # a pattern variable with the name of a column is not settled
    assert columns("FROM t |> MATCH_RECOGNIZE(ORDER BY a MEASURES COUNT(*) AS n PATTERN (a+) DEFINE a AS a > 0)") is None


def test_align():
    ts = Catalog.from_types({"m": {"ts": "TIMESTAMP", "n": "INT64", "job": "STRING"}})
    typed = infer(
        """FROM m |> ALIGN (TIMESTAMP ts PERIOD INTERVAL 1 MINUTE ORIGIN EPOCH PARTITION BY job
                          METRICS SUM(n) WITHIN (1 PERIOD PRECEDING) AS total)""",
        ts,
    )
    assert [(c.name, c.type.sql()) for c in typed.columns] == [
        ("job", "STRING"), ("total", "INT64"), ("ts", "TIMESTAMP")]


def test_recursive_cte_with_pipe_union():
    sql = "WITH RECURSIVE r AS ((SELECT 1 AS n) |> UNION ALL (SELECT n + 1 FROM r WHERE n < 3)) FROM r |> SELECT n"
    assert columns(sql) == [("n", "INT64")]
    wider = "WITH RECURSIVE r AS ((SELECT 1 AS n) |> UNION ALL (SELECT n + 1.5 FROM r WHERE n < 3)) FROM r |> SELECT n"
    assert columns(wider) == [("n", None)]


@pytest.mark.parametrize(
    "sql",
    [
        "FROM t |> CALL some_tvf()",
        "FROM t |> SELECT a |> FORK (|> SELECT a)",
        "FROM t |> SELECT AS STRUCT a, b",
        "FROM t |> SELECT AS VALUE s",
        "FROM t |> DROP nosuch",
        "FROM t |> RENAME a",
        "FROM t |> SET a = 1, a = 2",
        "FROM t |> SELECT a |>",
        "|> SELECT 1",
    ],
)
def test_what_is_not_modelled_is_unknown(sql):
    assert columns(sql) is None


def test_a_range_variable_named_like_a_column_is_left_unknown():
    assert columns("FROM t |> AS a |> SELECT a") is None  # the alias a and the column a
    assert columns("FROM t |> EXTEND 1 AS t |> SELECT t") is None  # t was a range variable of the FROM


def test_a_pipe_inside_a_string_or_comment_is_not_a_pipe():
    assert columns("SELECT '|>' AS s FROM t") == [("s", "STRING")]
    assert columns("SELECT a FROM t -- |> SELECT 1") == [("a", "INT64")]
    assert columns("FROM t /* |> */ |> SELECT a;") == [("a", "INT64")]


def test_pipe_findings_are_not_reported():
    typed = infer("FROM t |> SELECT a |> EXTEND nosuch + 1 AS z", CATALOG)
    assert [(c.name, c.type.sql() if c.type else None) for c in typed.columns] == [("a", "INT64"), ("z", None)]
    assert typed.findings == ()


def test_a_tree_that_sqlglot_rewrote_from_pipe_syntax_is_not_typed():
    import sqlglot

    tree = sqlglot.parse_one("FROM t |> SELECT a |> EXTEND a + 1 AS a1", read="bigquery")
    assert infer(tree, CATALOG).columns is None
