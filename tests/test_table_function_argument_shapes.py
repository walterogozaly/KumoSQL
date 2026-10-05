"""Table-valued function calls whose arguments are not just ``TABLE name``: expressions around a table argument, named
``TABLE`` and ``MODEL`` arguments, several tables, subqueries. Each parses, prints back as written and reads its tables.

``EXTERNAL_OBJECT_TRANSFORM(TABLE object_table, ['SO_UNKNOWN'])`` passes an array after the table. sqlglot 26 reads on after the
"Expecting )" it raises for the ``TABLE`` argument and crashes on what it is left with, so the call was unparseable there.
Forms BigQuery rejects (``TABLE t AS s``, ``TABLE t FOR SYSTEM_TIME AS OF ...``, ``fn(...) WITH OFFSET``) stay refused.
"""

from __future__ import annotations

import re

import pytest
import sqlglot
from sqlglot import exp

import kumosql  # noqa: F401  (installs the BigQuery syntax additions)
from kumosql.ast_utils import parse_statements
from kumosql.pipeline import Pipeline
from kumosql.pipeline_types import Model, Target

# (sql, tables it reads)
CALLS = [
    ("SELECT * FROM EXTERNAL_OBJECT_TRANSFORM(TABLE `p.d.src`, ['SO_UNKNOWN'])", {"p.d.src"}),
    ("SELECT * FROM EXTERNAL_OBJECT_TRANSFORM(TABLE `p.d.src`, ['SO_UNKNOWN', 'SO_OTHER']) AS t WHERE t.size > 1", {"p.d.src"}),
    ("SELECT * FROM `p.d.fn`(['a', 'b'], TABLE `p.d.src`)", {"p.d.src"}),
    ("SELECT * FROM `p.d.fn`(1, TABLE `p.d.src`, 2)", {"p.d.src"}),
    ("SELECT * FROM `p.d.fn`(TABLE `p.d.src`, TABLE `p.d.src2`, [1, 2], x => 1)", {"p.d.src", "p.d.src2"}),
    ("SELECT * FROM `p.d.fn`((SELECT 1 AS x), TABLE `p.d.src`)", {"p.d.src"}),
    ("SELECT * FROM `p.d.fn`(TABLE `p.d.src`, (SELECT MAX(x) FROM `p.d.src2`))", {"p.d.src", "p.d.src2"}),
    ("SELECT * FROM `p.d.fn`((SELECT x FROM `p.d.src2`), [1])", {"p.d.src2"}),
    ("SELECT * FROM `p.d.fn`(TABLE `p.d.src`, STRUCT(1 AS a, 'x' AS b))", {"p.d.src"}),
    ("SELECT * FROM `p.d.fn`(TABLE `p.d.src`, opts => STRUCT(1 AS a), cols => ['a', 'b'])", {"p.d.src"}),
    ("SELECT * FROM `p.d.fn`(MODEL `p.d.m`, TABLE `p.d.src`)", {"p.d.src"}),
    ("SELECT * FROM `p.d.fn`(MODEL `p.d.m`, TABLE `p.d.src`, STRUCT(1 AS k))", {"p.d.src"}),
    ("SELECT * FROM `p.d.fn`(TABLE `p.d.src`, MODEL `p.d.m`)", {"p.d.src"}),
    ("SELECT * FROM `p.d.fn`(a => MODEL `p.d.m`, b => TABLE `p.d.src`)", {"p.d.src"}),
    ("SELECT * FROM `p.d.fn`(a => TABLE `p.d.src`, b => [1])", {"p.d.src"}),
    ("SELECT * FROM `p.d.fn`(a => (SELECT 1), b => TABLE `p.d.src`)", {"p.d.src"}),
    ("SELECT * FROM `p.d.fn`(x => TABLE `p.d.src`, y => TABLE `p.d.src2`)", {"p.d.src", "p.d.src2"}),
    ("SELECT * FROM `p.d.fn`(TABLE `p.d.src`, x => [1, 2], y => (SELECT 1), z => TABLE `p.d.src2`)", {"p.d.src", "p.d.src2"}),
    ("SELECT * FROM `p.d.fn`(TABLE `p.d.src`, [1]) AS f JOIN `p.d.fn2`(TABLE `p.d.src2`, ['x']) AS g ON f.id = g.id", {"p.d.src", "p.d.src2"}),
    ("WITH c AS (SELECT * FROM `p.d.fn`(TABLE `p.d.src`, ['x'])) SELECT * FROM c", {"p.d.src"}),
    ("SELECT * FROM `p.d.fn`(TABLE `p.d.src`, [1]) TABLESAMPLE SYSTEM (10 PERCENT)", {"p.d.src"}),
    ("SELECT 1 FROM `p.d.src2` WHERE id IN (SELECT id FROM `p.d.fn`(TABLE `p.d.src`, [1]))", {"p.d.src", "p.d.src2"}),
    ("SELECT * FROM `p.d.src2` LEFT JOIN `p.d.fn`(TABLE `p.d.src`, [1]) USING (id)", {"p.d.src", "p.d.src2"}),
    ("SELECT * FROM `p.d.src2` AS a JOIN EXTERNAL_OBJECT_TRANSFORM(TABLE `p.d.src`, ['SO_UNKNOWN']) AS b ON a.uri = b.uri", {"p.d.src", "p.d.src2"}),
    ("SELECT * FROM `p.d.fn`(TABLE `p.d.src`, ['table'])", {"p.d.src"}),
    ("SELECT * FROM `p.d.fn`(TABLE `p.d.src`, 'TABLE x')", {"p.d.src"}),
    ("SELECT * FROM AI.GENERATE_TABLE(MODEL `p.d.m`, TABLE `p.d.src`, STRUCT('s STRING' AS output_schema))", {"p.d.src"}),
    ("SELECT * FROM VECTOR_SEARCH(TABLE `p.d.src`, ['a', 'b'], TABLE `p.d.src2`)", {"p.d.src", "p.d.src2"}),
    ("SELECT * FROM ML.EVALUATE(MODEL `p.d.m`, TABLE `p.d.src`, STRUCT(0.5 AS threshold))", {"p.d.src"}),
]
SOURCES = {f"p.d.{n}": Target("p", "d", n) for n in ("src", "src2")}
SCHEMAS = {"p.d.src": {"id": "INT64"}, "p.d.src2": {"id": "INT64"}}


def squash(sql: str) -> str:
    return re.sub(r"\s+", "", sql)


def reads(sql: str) -> set[str]:
    pipeline = Pipeline({"p.d.target": Model(Target("p", "d", "target"), "table", sql)}, SOURCES, SCHEMAS)
    assert not {"parse_error", "no_query", "unknown_reads"} & {d.code for d in pipeline.all_diagnostics()}
    return {name for name in pipeline.upstream["p.d.target"]}


@pytest.mark.parametrize("sql,tables", CALLS)
def test_the_call_parses_and_prints_back_unchanged(sql, tables):
    (statement,) = parse_statements(sql)
    assert squash(statement.sql("bigquery")) == squash(sql)


@pytest.mark.parametrize("sql,tables", CALLS)
def test_the_tables_it_names_are_read(sql, tables):
    assert reads(sql) == tables


def test_a_table_argument_is_a_table_node_and_a_model_argument_is_not():
    (statement,) = parse_statements("SELECT * FROM `p.d.fn`(a => MODEL `p.d.m`, b => TABLE `p.d.src`, c => TABLE `p.d.src2`)")
    names = {table.name for table in statement.find_all(exp.Table)}
    assert {"src", "src2"} <= names and "m" not in names


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT * FROM `p.d.fn`(TABLE `p.d.src` FOR SYSTEM_TIME AS OF TIMESTAMP '2024-01-01')",
        "SELECT * FROM `p.d.fn`(TABLE `p.d.src` s, [1])",
        "SELECT * FROM `p.d.fn`(TABLE `p.d.src`, [1]) WITH OFFSET AS o",
    ],
)
def test_what_bigquery_rejects_is_still_refused(sql):
    with pytest.raises(sqlglot.errors.ParseError):
        parse_statements(sql)


def test_a_column_called_model_is_not_a_model_argument():
    sql = "SELECT a, model m, b FROM `p.d.src`"
    assert squash(parse_statements(sql)[0].sql("bigquery")) == squash("SELECT a, model AS m, b FROM `p.d.src`")
    sql = "SELECT COALESCE(model, 'x') AS m, f(a, model) AS n FROM `p.d.src`"
    assert squash(parse_statements(sql)[0].sql("bigquery")) == squash(sql)


def test_a_failure_that_is_not_a_parse_error_is_reported_as_one():
    # sqlglot 26 crashes with an AttributeError on this once it has read past the "Expecting )" it raised.
    with pytest.raises(sqlglot.errors.ParseError):
        parse_statements("SELECT * FROM `p.d.fn`(TABLE `p.d.src` s, [1]) f, g")
