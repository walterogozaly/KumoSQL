"""BigQuery statements sqlglot rejects that kumosql.bigquery_syntax reads (TABLE arguments are in test_table_function_arguments.py)."""

import pytest
import sqlglot
import sqlglot.tokens
from sqlglot import exp

import kumosql  # noqa: F401  (installs the BigQuery syntax additions)


@pytest.mark.parametrize(
    "sql",
    [
        "DROP TABLE FUNCTION `p.d.f`",
        "DROP TABLE FUNCTION IF EXISTS `p.d.f`",
        "drop table function if exists d.f",
    ],
)
def test_drop_table_function_is_a_drop_of_that_kind(sql):
    tree = sqlglot.parse_one(sql, read="bigquery")
    assert isinstance(tree, exp.Drop) and tree.args.get("kind") == "TABLE FUNCTION"
    assert tree.sql("bigquery").upper() == " ".join(sql.split()).upper()


def test_drop_table_and_drop_function_are_unchanged():
    table, function = sqlglot.parse("DROP TABLE `p.d.t`; DROP FUNCTION `p.d.f`", read="bigquery")
    assert table.args.get("kind") == "TABLE" and function.args.get("kind") == "FUNCTION"


def test_a_script_with_a_dropped_table_function_still_reads_its_other_statements():
    trees = sqlglot.parse("DROP TABLE FUNCTION IF EXISTS `p.d.f`;\nSELECT a FROM fn(TABLE `p.d.t`)", read="bigquery")
    assert trees[0].args.get("kind") == "TABLE FUNCTION"
    assert "t" in [table.name for table in trees[1].find_all(exp.Table)]


def _projections(tree):
    return [select.sql("bigquery") for select in tree.find_all(exp.Select)]


needs_pipes = pytest.mark.skipif(not hasattr(sqlglot.tokens.TokenType, "PIPE_GT"), reason="this sqlglot has no pipe syntax")


@needs_pipes
def test_pipe_set_and_drop_read_as_replace_and_except():
    tree = sqlglot.parse_one("FROM d.t |> SET state = UPPER(state), n = 1 |> DROP city, x", read="bigquery")
    selects = " ".join(_projections(tree))
    assert "* REPLACE (UPPER(state) AS state, 1 AS n)" in selects and "* EXCEPT (city, x)" in selects


@needs_pipes
def test_pipe_set_inside_a_subquery_stops_at_its_parenthesis():
    tree = sqlglot.parse_one("SELECT * FROM (FROM d.t |> DROP a) AS s |> WHERE b > 1", read="bigquery")
    assert "* EXCEPT (a)" in " ".join(_projections(tree)) and tree.find(exp.Where) is not None


@needs_pipes
def test_pipe_with_moves_to_the_front_of_its_query():
    sql = "SELECT * FROM (FROM d.t |> WITH y AS (SELECT 2 AS z), w AS (SELECT * FROM y) |> CROSS JOIN w) |> WITH v AS (SELECT 1 AS q) |> CROSS JOIN v"
    tree = sqlglot.parse_one(sql, read="bigquery")
    assert {cte.alias for cte in tree.find_all(exp.CTE)} >= {"y", "w", "v"}
    assert {table.name for table in tree.find_all(exp.Table)} >= {"t", "w", "v"}


@needs_pipes
@pytest.mark.parametrize(
    "sql",
    [
        "FROM y |> WITH y AS (SELECT 2 AS z) |> CROSS JOIN y",  # the name already means a table before the WITH
        "FROM d.t |> WHERE `Y` > 1 |> WITH y AS (SELECT 2 AS z) |> CROSS JOIN y",
        "FROM d.t |> WITH y AS (SELECT 1) |> CROSS JOIN y |> WITH y AS (SELECT 2) |> CROSS JOIN y",
        "WITH a AS (SELECT 1 AS x) FROM a |> WITH y AS (SELECT 2 AS z) |> CROSS JOIN y",
        "FROM d.t |> WITH RECURSIVE y AS (SELECT 1) |> CROSS JOIN y",
    ],
)
def test_pipe_with_stays_unread_when_moving_it_could_change_a_name(sql):
    with pytest.raises(sqlglot.errors.ParseError):
        sqlglot.parse_one(sql, read="bigquery")


def test_raw_bytes_read_as_the_same_bytes():
    # BigQuery: br'a\d' = b'a\\d', the bytes a, backslash, d
    tree = sqlglot.parse_one("SELECT br'a\\d' AS x, RB\"q'\" AS y", read="bigquery")
    assert tree.sql("bigquery") == r"SELECT b'a\x5Cd' AS x, b'q\x27' AS y"


def test_pipe_rename_is_not_guessed():
    # RENAME keeps the column in place, which no SELECT can say without the column list.
    with pytest.raises(sqlglot.errors.ParseError):
        sqlglot.parse_one("FROM d.t |> RENAME a AS b", read="bigquery")


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT * FROM GRAPH_TABLE(`p.d.g` MATCH (a)-[e]->(b) RETURN a.id AS a_id, b.id AS b_id)",
        "SELECT g.a, t.b FROM GRAPH_TABLE(d.g MATCH (n:Person {name: 'O\\'Brien'})\n\tRETURN n.id AS a) AS g JOIN d.t AS t ON g.a = t.a",
    ],
)
def test_graph_table_prints_back_as_written_and_is_not_a_table(sql):
    tree = sqlglot.parse_one(sql, read="bigquery")
    assert tree.sql("bigquery") == sql
    names = {table.name for table in tree.find_all(exp.Table) if table.name}
    assert names <= {"t"}


@pytest.mark.parametrize(
    "sql,tables",
    [
        ("SELECT * FROM ML.EVALUATE(MODEL `p.d.m`, TABLE `p.d.t`)", {"t"}),
        ("SELECT * FROM ML.EVALUATE(MODEL `p.d.m`)", set()),
        ("SELECT * FROM ML.DETECT_ANOMALIES(MODEL `p.d.m`, STRUCT(0.01 AS contamination), TABLE `p.d.t`)", {"t"}),
        ("SELECT * FROM ML.EXPLAIN_PREDICT(MODEL `p.d.m`, (SELECT * FROM p.d.t), STRUCT(3 AS top_k_features))", {"t"}),
    ],
)
def test_ml_functions_sqlglot_does_not_know_read_their_tables_not_the_model(sql, tables):
    tree = sqlglot.parse_one(sql, read="bigquery")
    assert tree.sql("bigquery") == sql
    assert {table.name for table in tree.find_all(exp.Table) if table.name} == tables


def test_ml_functions_sqlglot_reads_itself_are_unchanged():
    # A query that fails for another reason is retried; ML.PREDICT must still come out of sqlglot's own parser.
    sql = "SELECT * FROM ML.PREDICT(MODEL `p.d.m`, TABLE `p.d.t`) AS p JOIN ML.EVALUATE(MODEL `p.d.m`, TABLE `p.d.u`) AS e ON TRUE"
    tree = sqlglot.parse_one(sql, read="bigquery")
    assert tree.sql("bigquery") == sql
    assert type(tree.find(exp.Predict)).__name__ == "Predict"
