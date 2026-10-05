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


@pytest.mark.parametrize(
    "sql",
    ["SELECT 'a\nb' AS v", 'SELECT "a\r\nb"', "SELECT b'x\ny'", "SELECT r'p\nq'", "SELECT rb'm\nn'", "SELECT `c\nd` FROM t"],
)
def test_a_one_line_literal_with_a_line_break_is_rejected_like_bigquery_does(sql):
    # BigQuery: "Syntax error: Unclosed string literal" (and bytes, identifier); only triple-quoted literals span lines.
    with pytest.raises(sqlglot.errors.ParseError, match="Unclosed literal"):
        sqlglot.parse(sql, read="bigquery")


def test_triple_quoted_literals_and_escaped_line_breaks_still_read():
    tree = sqlglot.parse_one("SELECT '''a\nb''' AS x, \"\"\"c\nd\"\"\" AS y, 'e\\nf' AS z\nFROM t", read="bigquery")
    assert [e.this.this for e in tree.expressions] == ["a\nb", "c\nd", "e\nf"]
    # a command such as EXECUTE IMMEDIATE keeps its remaining text as one STRING token that spans the line break
    assert sqlglot.parse_one("EXECUTE IMMEDIATE 'SELECT ? + ?' USING 1, 2\n", read="bigquery") is not None


def _printed(sql):
    return sqlglot.parse_one(sql, read="bigquery").sql("bigquery")


@pytest.mark.parametrize(
    "sql,printed,quantifier",
    [
        ("SELECT s LIKE ALL UNNEST(['a%', '%b']) FROM t", "SELECT s LIKE ALL UNNEST(['a%', '%b']) FROM t", exp.All),
        ("SELECT s NOT LIKE ALL UNNEST(arr) FROM t", "SELECT NOT s LIKE ALL UNNEST(arr) FROM t", exp.All),
        ("SELECT s LIKE SOME UNNEST(['a%']) FROM t", "SELECT s LIKE ANY UNNEST(['a%']) FROM t", exp.Any),
    ],
)
def test_like_quantified_over_unnest(sql, printed, quantifier):
    tree = sqlglot.parse_one(sql, read="bigquery")
    text = tree.sql("bigquery")
    assert text in (printed, printed.replace("SELECT NOT s", "SELECT s NOT"))  # sqlglot 26 puts NOT in front
    assert sqlglot.parse_one(text, read="bigquery").sql("bigquery") == text
    assert not list(tree.find_all(exp.Anonymous))  # no marker is left in the tree
    assert tree.find(quantifier) is not None and isinstance(tree.find(quantifier).this, exp.Unnest)


def test_like_all_is_not_like_any():
    every = sqlglot.parse_one("SELECT s LIKE ALL UNNEST(['a']) FROM t", read="bigquery")
    some = sqlglot.parse_one("SELECT s LIKE ANY UNNEST(['a']) FROM t", read="bigquery")
    assert every.find(exp.All) is not None and every.find(exp.Any) is None
    assert some.find(exp.Any) is not None and some.find(exp.All) is None


def test_aggregate_where_is_a_filter_and_prints_back_inside_the_call():
    tree = sqlglot.parse_one("SELECT COUNT(* WHERE x > 1) AS n, SUM(DISTINCT y WHERE x > 1) AS s, ARRAY_AGG(z IGNORE NULLS WHERE x > 1) AS a FROM t", read="bigquery")
    filters = list(tree.find_all(exp.Filter))
    assert [type(f.this).__name__ for f in filters] == ["Count", "Sum", "ArrayAgg"]
    assert all(isinstance(f.expression, exp.Where) for f in filters)
    assert tree.sql("bigquery") == (
        "SELECT COUNT(* WHERE x > 1) AS n, SUM(DISTINCT y WHERE x > 1) AS s, ARRAY_AGG(z IGNORE NULLS WHERE x > 1) AS a FROM t"
    )


def test_aggregate_where_reads_its_columns():
    tree = sqlglot.parse_one("SELECT SUM(y WHERE x > 1) FROM t", read="bigquery")
    assert {column.name for column in tree.find_all(exp.Column)} == {"x", "y"}


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT SUM(x WHERE y) OVER () FROM t",  # BigQuery: "WHERE clause is not supported inside Window function call"
        "SELECT SUM(x WHERE y) OVER (PARTITION BY z) FROM t",
        "SELECT STRING_AGG(x, ',' WHERE y ORDER BY x) FROM t",  # BigQuery: not supported with ORDER BY
        "SELECT ARRAY_AGG(x WHERE y LIMIT 2) FROM t",
        "SELECT (x WHERE y) FROM t",
        "SELECT IN (a WHERE b) FROM t",
    ],
)
def test_aggregate_where_where_bigquery_rejects_it_stays_unread(sql):
    with pytest.raises(sqlglot.errors.ParseError):
        sqlglot.parse_one(sql, read="bigquery")


def test_a_where_in_a_subquery_argument_is_not_an_aggregate_filter():
    tree = sqlglot.parse_one("SELECT ARRAY(SELECT x FROM t WHERE y), EXISTS(SELECT 1 FROM u WHERE z) FROM v", read="bigquery")
    assert tree.find(exp.Filter) is None and len(list(tree.find_all(exp.Where))) == 2


def test_a_filter_from_another_dialect_prints_inside_the_call_only_when_bigquery_allows_it():
    tree = sqlglot.parse_one("SELECT COUNT(*) FILTER (WHERE x > 1) FROM t", read="postgres")
    assert tree.sql("bigquery") == "SELECT COUNT(* WHERE x > 1) FROM t"
    windowed = sqlglot.parse_one("SELECT COUNT(*) FILTER (WHERE x > 1) OVER () FROM t", read="postgres")
    assert "FILTER" in windowed.sql("bigquery")


def test_with_expression_variables_are_not_columns():
    tree = sqlglot.parse_one("SELECT WITH(a AS t.c + 1, b AS a * 2, a + b) AS w FROM t", read="bigquery")
    assert tree.sql("bigquery") == "SELECT WITH(a AS t.c + 1, b AS a * 2, a + b) AS w FROM t"
    assert [column.sql() for column in tree.find_all(exp.Column)] == ["t.c"]
    assert sorted(variable.name for variable in tree.find_all(exp.Var)) == ["a", "a", "b"]


def test_with_expression_names_that_are_columns_outside_it_stay_columns():
    tree = sqlglot.parse_one("SELECT a, WITH(b AS a, b + a) AS w, WITH(a AS 1, a) AS v FROM t", read="bigquery")
    # in the first expression ``a`` is never a variable, so all three are columns of ``t``; in the second it is the variable
    assert [column.name for column in tree.find_all(exp.Column)] == ["a", "a", "a"]
    assert sorted(variable.name for variable in tree.find_all(exp.Var)) == ["a", "b"]
    assert tree.sql("bigquery") == "SELECT a, WITH(b AS a, b + a) AS w, WITH(a AS 1, a) AS v FROM t"


def test_with_expression_may_nest_and_hold_a_subquery():
    tree = sqlglot.parse_one("SELECT WITH(a AS (SELECT MAX(x) FROM t), WITH(b AS a + 1, a + b))", read="bigquery")
    assert tree.sql("bigquery") == "SELECT WITH(a AS (SELECT MAX(x) FROM t), WITH(b AS a + 1, a + b))"
    assert [column.name for column in tree.find_all(exp.Column)] == ["x"]


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT WITH(a AS 1, a AS 2, a)",  # defined twice
        "SELECT WITH(a AS 1, (SELECT a FROM t))",  # a subquery could mean a column of its own
        "SELECT WITH(a)",  # no definition
        "SELECT WITH(1, 2)",
    ],
)
def test_with_expression_that_cannot_be_read_faithfully_is_refused(sql):
    with pytest.raises(sqlglot.errors.ParseError):
        sqlglot.parse_one(sql, read="bigquery")


def test_a_cte_is_not_a_with_expression():
    tree = sqlglot.parse_one("WITH a AS (SELECT 1 AS x) SELECT x FROM a", read="bigquery")
    assert [cte.alias for cte in tree.find_all(exp.CTE)] == ["a"]
    assert not list(tree.find_all(exp.Anonymous))


@pytest.mark.parametrize(
    "sql,path",
    [
        ("SELECT e, o FROM t, t.arr e WITH OFFSET o", "t.arr"),
        ("SELECT e FROM t, t.a.b AS e WITH OFFSET AS o", "t.a.b"),
        ("SELECT e FROM t JOIN t.arr e WITH OFFSET ON TRUE", "t.arr"),
        ("SELECT e FROM t, t.arr WITH OFFSET", "t.arr"),
    ],
)
def test_array_path_with_offset_is_an_unnest(sql, path):
    tree = sqlglot.parse_one(sql, read="bigquery")
    unnest = tree.find(exp.Unnest)
    assert unnest is not None and unnest.args.get("offset") is not None
    assert [column.sql() for column in unnest.expressions] == [path]
    assert [table.name for table in tree.find_all(exp.Table)] == ["t"]  # t.arr is a path, not a table


def test_a_table_with_offset_is_not_guessed_at():
    with pytest.raises(sqlglot.errors.ParseError):
        sqlglot.parse_one("SELECT * FROM arr WITH OFFSET o", read="bigquery")


@pytest.mark.parametrize("sql", ["SELECT STRUCT<>()", "SELECT STRUCT<>(1) AS s", "SELECT ARRAY_AGG(STRUCT<>())"])
def test_empty_struct_type_is_refused_not_read_as_a_comparison(sql):
    with pytest.raises(sqlglot.errors.ParseError):
        sqlglot.parse_one(sql, read="bigquery")


def test_ordinary_not_equal_and_typed_structs_are_unchanged():
    assert _printed("SELECT a <> b FROM t") == "SELECT a <> b FROM t"
    assert "STRUCT<a INT64>" in _printed("SELECT STRUCT<a INT64>(1)")
    assert _printed("SELECT STRUCT(1 AS a)") == "SELECT STRUCT(1 AS a)"
