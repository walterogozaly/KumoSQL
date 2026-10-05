"""Checks for the QED Calcite fixture produced by tools/qed_to_sql.py."""

import json
from pathlib import Path

import pytest
import sqlglot

FIXTURES = Path(__file__).parent / "fixtures" / "qed"
CASES = [json.loads(line) for line in (FIXTURES / "qed_calcite_pairs.jsonl").read_text().splitlines()]


def test_fixture_loads():
    assert len(CASES) > 300
    assert len({c["name"] for c in CASES}) == len(CASES)
    for c in CASES:
        assert {"name", "sql_a", "sql_b", "ddl", "schema_id", "schemas"} <= set(c)
        # a few cases only use VALUES and declare no tables
        assert c["ddl"].startswith("CREATE TABLE") == bool(c["schemas"])


def test_summary_matches_fixture():
    summary = json.loads((FIXTURES / "summary.json").read_text())
    skipped = (FIXTURES / "qed_calcite_skipped.jsonl").read_text().splitlines()
    assert summary["converted"] == len(CASES)
    assert summary["skipped"] == len(skipped)
    assert all(json.loads(s)["reason"] for s in skipped)


@pytest.mark.parametrize("case", CASES, ids=[c["name"] for c in CASES])
def test_pair_parses(case):
    for key in ("sql_a", "sql_b"):
        tree = sqlglot.parse_one(case[key], read="mysql")
        assert isinstance(tree, sqlglot.exp.Query)
    for stmt in sqlglot.parse(case["ddl"], read="mysql") if case["ddl"] else []:
        assert isinstance(stmt, sqlglot.exp.Create)


def test_duckdb_runs_a_sample():
    duckdb = pytest.importorskip("duckdb")
    for case in CASES[:25]:
        con = duckdb.connect()
        for stmt in sqlglot.transpile(case["ddl"], read="mysql", write="duckdb"):
            con.execute(stmt)
        for key in ("sql_a", "sql_b"):
            con.execute(sqlglot.transpile(case[key], read="mysql", write="duckdb")[0]).fetchall()


def test_windows_group_sets_and_clock_pairs_are_converted_exactly():
    names = {c["name"] for c in CASES}
    for name in ("testJoinProjectTransposeWindow", "testPushProjectWithOverPastJoin1", "testAggregateDynamicFunction", "testAggregateJoinRemove10", "testReduceConstants"):
        assert name in names
    by_name = {c["name"]: c for c in CASES}
    assert "OVER (" in by_name["testPushProjectWithOverPastJoin1"]["sql_a"]
    assert "CURRENT_TIMESTAMP" in by_name["testAggregateDynamicFunction"]["sql_a"]


def test_filter_and_grouping_sets_pairs_stay_skipped_with_the_sharper_reason():
    skipped = {json.loads(s)["name"]: json.loads(s)["reason"] for s in (FIXTURES / "qed_calcite_skipped.jsonl").read_text().splitlines()}
    assert "FILTER" in skipped["testDistinctCountGroupingSets1"] and "grouping sets" in skipped["testDistinctCountGroupingSets1"]
