"""``FROM dataset.fn(TABLE input, option => value)``: a table passed to a table-valued function parses, is read and prints back unchanged.

sqlglot's BigQuery parser stops at ``TABLE`` ("Expecting )"), so a model with such a call was unparseable and lost every read.
"""

from __future__ import annotations

import pytest
import sqlglot

import kumosql
from kumosql.pipeline import Pipeline
from kumosql.pipeline_types import Model, Target

CALLS = [
    "SELECT * FROM `p.d.fn`(TABLE `p.d.src`, mode => 'x')",
    "SELECT * FROM `p.d.fn`(TABLE `p.d.src`)",
    "SELECT * FROM `p.d.fn`(TABLE `p.d.src`, TABLE `p.d.src2`, threshold => 0.5)",
    "SELECT * FROM `p.d.fn`(TABLE (SELECT id FROM `p.d.src`), n => 3)",
    "SELECT z.id FROM `p.d.fn`(TABLE `p.d.src`, zip_type => zip_type, other => 'x') AS z WHERE z.id > 1",
    "SELECT a.id FROM `p.d.src2` AS a JOIN `p.d.fn`(TABLE `p.d.src`, mode => 'x') AS f ON a.id = f.id",
    "WITH f AS (SELECT * FROM `p.d.fn`(TABLE `p.d.src`, mode => 'x')) SELECT * FROM f",
    "CREATE OR REPLACE TABLE `p.d.target` AS SELECT * FROM `p.d.fn`(TABLE `p.d.src`, mode => 'x')",
]
SOURCES = {f"p.d.{n}": Target("p", "d", n) for n in ("src", "src2")}


def pipeline(sql: str, kind: str = "table") -> Pipeline:
    return Pipeline({"p.d.target": Model(Target("p", "d", "target"), kind, sql)}, SOURCES, {"p.d.src": {"id": "INT64"}, "p.d.src2": {"id": "INT64"}})


@pytest.mark.parametrize("sql", CALLS)
def test_the_call_parses_and_prints_back_in_bigquery(sql):
    assert sqlglot.parse_one(sql, read="bigquery").sql("bigquery").replace(" ", "") == sql.replace(" ", "")


@pytest.mark.parametrize("sql", CALLS)
def test_the_table_arguments_are_reads_and_nothing_fails(sql):
    pl = pipeline(sql)
    codes = {d.code for d in pl.all_diagnostics()}
    assert not {"parse_error", "no_query", "unknown_reads"} & codes
    reads = pl.upstream["p.d.target"]
    assert "p.d.src" in reads
    assert ("p.d.src2" in reads) == ("src2" in sql)


def test_columns_a_function_returns_are_unknown_not_attributed_to_a_table_named_like_the_function():
    pl = pipeline("SELECT z.a, s.id FROM `p.d.fn`(TABLE `p.d.src`, mode => 'x') AS z JOIN `p.d.src2` s ON z.a = s.id")
    status = {r["column"]: r["status"] for r in pl.lineage_report()}
    assert status == {"a": "unknown", "id": "traced"}
    assert all(not ref.table.endswith(".fn") for sources in pl.column_lineage().values() for ref in sources)


def test_ml_predict_still_reads_its_table_and_prints_back():
    sql = "SELECT * FROM ML.PREDICT(MODEL `p.d.m`, TABLE `p.d.src`, STRUCT(0.5 AS threshold))"
    assert sqlglot.parse_one(sql, read="bigquery").sql("bigquery") == sql
    assert "p.d.src" in pipeline(sql).upstream["p.d.target"]


def test_rules_and_formatting_keep_the_call_intact():
    sql = "SELECT z.id FROM `p.d.fn`(TABLE `p.d.src`, zip_type => zip_type) AS z WHERE 1 = 1"
    rewritten = kumosql.apply_rules(list(kumosql.available_rules()), sql).sql
    assert "TABLE `p.d.src`" in rewritten and "zip_type => zip_type" in rewritten
    assert "TABLE `p.d.src`" in kumosql.format_sql(sql)
