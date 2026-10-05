"""Calls and clauses sqlglot read into something other than what was written (found by the round-two syntax sweep).

Each is read faithfully, so the tables it names are reads and what it prints back means what was written.
"""

from __future__ import annotations

import re

import pytest
import sqlglot
from sqlglot import exp

import kumosql  # noqa: F401  (installs the BigQuery syntax additions)
from kumosql.ast_utils import parse_statements


def squash(sql: str) -> str:
    return re.sub(r"\s+", "", sql)


def tables(sql: str) -> set[str]:
    (statement,) = parse_statements(sql)
    return {table.name for table in statement.find_all(exp.Table) if not isinstance(table.this, exp.Func)}


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT AI.GENERATE_BOOL('is it ok?') AS b",
        "SELECT AI.GENERATE_BOOL(CONCAT('is ', c, ' ok?'), connection_id => 'x') AS b FROM `p.d.t`",
        "SELECT AI.GENERATE_INT('how many?', connection_id => 'x') AS n",
        "SELECT AI.GENERATE_DOUBLE('how much?') AS n",
        "SELECT AI.GENERATE_TEXT('say hi') AS s",
        "SELECT AI.IF(('is it?', c)) AS b FROM `p.d.t`",
        "SELECT c FROM `p.d.t` WHERE AI.IF(('is it?', c), connection_id => 'x')",
    ],
)
def test_a_scalar_ai_call_is_a_function_and_its_prompt_is_not_a_table(sql):
    (statement,) = parse_statements(sql)
    assert squash(statement.sql("bigquery")) == squash(sql)
    assert tables(sql) <= {"t"}


@pytest.mark.parametrize(
    "sql, read",
    [
        ("SELECT * FROM AI.GENERATE_BOOL(MODEL `p.d.m`, TABLE `p.d.src`)", {"m", "src"}),
        ("SELECT * FROM AI.GENERATE_TEXT(MODEL `p.d.m`, (SELECT 'x' AS prompt FROM `p.d.src`), STRUCT(1 AS k))", {"m", "src"}),
    ],
)
def test_the_table_function_forms_are_still_the_table_functions(sql, read):
    (statement,) = parse_statements(sql)
    assert squash(statement.sql("bigquery")) == squash(sql)
    assert read <= tables(sql)


@pytest.mark.parametrize(
    "sql, read",
    [
        ("SELECT * FROM AI.FORECAST(TABLE `p.d.src`, connection_id => 'c', horizon => 3)", {"src"}),
        ("SELECT * FROM AI.FORECAST(TABLE `p.d.src`, data_col => 'v', timestamp_col => 't', model => 'm', some_new_option => TRUE)", {"src"}),
        ("SELECT * FROM AI.FORECAST((SELECT v, t FROM `p.d.src`), data_col => 'v', timestamp_col => 't', other => 1)", {"src"}),
        ("SELECT * FROM VECTOR_SEARCH(TABLE `p.d.src`, 'emb', TABLE `p.d.q`, top_k => 2, new_option => 1)", {"src", "q"}),
        ("SELECT * FROM ML.FEATURES_AT_TIME(TABLE `p.d.src`, time => TIMESTAMP '2024-01-01', other => 1)", {"src"}),
    ],
)
def test_an_argument_sqlglot_has_no_slot_for_is_kept(sql, read):
    (statement,) = parse_statements(sql)
    printed = statement.sql("bigquery")
    for name in re.findall(r"(\w+) =>", sql):
        assert name in printed
    assert read <= tables(sql)


def test_two_calls_that_differ_in_an_unknown_argument_do_not_read_alike():
    one = parse_statements("SELECT * FROM AI.FORECAST(TABLE `p.d.src`, connection_id => 'a')")[0]
    two = parse_statements("SELECT * FROM AI.FORECAST(TABLE `p.d.src`, connection_id => 'b')")[0]
    assert one != two and one.sql("bigquery") != two.sql("bigquery")


def test_a_call_with_only_known_arguments_keeps_its_own_node():
    (statement,) = parse_statements("SELECT * FROM AI.FORECAST(TABLE `p.d.src`, data_col => 'v', horizon => 3)")
    assert statement.find(exp.AIForecast) is not None or not hasattr(exp, "AIForecast")


def test_a_sample_after_time_travel_belongs_to_the_table():
    sql = "SELECT * FROM `p.d.t` FOR SYSTEM_TIME AS OF CURRENT_TIMESTAMP() TABLESAMPLE SYSTEM (10 PERCENT) LIMIT 1"
    (statement,) = parse_statements(sql)
    table = statement.find(exp.Table)
    assert table.args.get("sample") is not None and table.args.get("version") is not None
    assert statement.args.get("sample") is None
    assert "LIMIT 1 TABLESAMPLE" not in statement.sql("bigquery")


def test_a_sample_after_time_travel_of_a_joined_table_stays_with_that_table():
    (statement,) = parse_statements(
        "SELECT * FROM `p.d.a` AS a JOIN `p.d.b` AS b FOR SYSTEM_TIME AS OF TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL 1 HOUR) TABLESAMPLE SYSTEM (10 PERCENT) USING (id)"
    )
    sampled = [table.name for table in statement.find_all(exp.Table) if table.args.get("sample") is not None]
    assert sampled == ["b"]


def test_a_sample_without_time_travel_is_untouched():
    sql = "SELECT * FROM `p.d.t` AS t TABLESAMPLE SYSTEM (10 PERCENT) LIMIT 1"
    (statement,) = parse_statements(sql)
    assert squash(statement.sql("bigquery")) == squash(sql)


def test_ordinary_calls_named_like_these_are_untouched():
    sql = "SELECT forecast(a), vector_search, ai FROM `p.d.t`"
    assert squash(parse_statements(sql)[0].sql("bigquery")) == squash(sql)
    assert squash(sqlglot.parse_one("SELECT ai.x, ai.y FROM ai", read="bigquery").sql("bigquery")) == squash("SELECT ai.x, ai.y FROM ai")


@pytest.mark.parametrize(
    "sql, read",
    [
        ("SELECT forecast(a) AS f, my_dataset.predict(b) AS p FROM `p.d.t`", {"t"}),
        ("SELECT * FROM `p.d.forecast`(TABLE `p.d.src`, [1], horizon => 3)", {"src"}),
        ("SELECT * FROM d.generate_text(TABLE `p.d.src`, 'x')", {"src"}),
    ],
)
def test_a_function_of_the_projects_own_named_like_an_ml_function_is_a_function(sql, read):
    (statement,) = parse_statements(sql)
    assert squash(statement.sql("bigquery")) == squash(sql)
    assert tables(sql) == read
