"""Checks for the Calcite-mined fixture produced by tools/calcite_plan_to_sql.py."""

import json
import sys
from pathlib import Path

import pytest
import sqlglot

FIXTURES = Path(__file__).parent / "fixtures" / "calcite_mined"
CASES = [json.loads(line) for line in (FIXTURES / "pairs.jsonl").read_text().splitlines() if line.strip()]
SKIPPED = [json.loads(line) for line in (FIXTURES / "skipped.jsonl").read_text().splitlines() if line.strip()]
SCHEMAS = json.loads((FIXTURES / "schemas.json").read_text())
SUMMARY = json.loads((FIXTURES / "summary.json").read_text())

FIELDS = {"name", "calcite_commit", "sql_a", "sql_b", "schema_id", "in_sqlsolver", "in_qed", "in_rbot", "new"}


def test_fixture_loads():
    assert len(CASES) > 400
    assert len({c["name"] for c in CASES}) == len(CASES)
    names = {c["name"] for c in CASES} | {s["name"] for s in SKIPPED}
    assert len(names) == len(CASES) + len(SKIPPED)  # every test is either a pair or a skip
    for c in CASES:
        assert FIELDS <= set(c)
        assert c["calcite_commit"] == SUMMARY["calcite_commit"]
        assert c["schema_id"] in SCHEMAS
        assert c["new"] == (not (c["in_sqlsolver"] or c["in_qed"] or c["in_rbot"]))
        assert c["sql_a"] != c["sql_b"]


def test_summary_matches_fixture():
    assert SUMMARY["tests"] == len(CASES) + len(SKIPPED)
    assert SUMMARY["translated"] == len(CASES)
    assert SUMMARY["skipped"] + SUMMARY["unchanged"] == len(SKIPPED)
    assert SUMMARY["translated_coverage"]["new"] == sum(c["new"] for c in CASES)
    assert all(s["reason"] for s in SKIPPED)
    assert sum(SUMMARY["skip_reasons"].values()) == SUMMARY["skipped"]
    val = SUMMARY["duckdb_validation"]
    assert val["differing"] == [c["name"] for c in CASES if c["differs_in_duckdb"]]
    counter = [json.loads(line)["name"] for line in (FIXTURES / "duckdb_counterexamples.jsonl").read_text().splitlines()]
    assert counter == val["differing"]


def test_license_shipped():
    assert "Apache License" in (FIXTURES / "LICENSE").read_text()
    assert "Apache Calcite" in (FIXTURES / "NOTICE").read_text()


@pytest.mark.parametrize("case", CASES, ids=[c["name"] for c in CASES])
def test_pair_parses(case):
    for key in ("sql_a", "sql_b"):
        tree = sqlglot.parse_one(case[key], read="mysql")
        assert isinstance(tree, sqlglot.exp.Query)


@pytest.mark.parametrize("sid", sorted(SCHEMAS))
def test_schema_parses(sid):
    ddl = SCHEMAS[sid]["ddl"]
    assert bool(ddl) == bool(SCHEMAS[sid]["tables"])  # pairs over VALUES only declare no tables
    for stmt in sqlglot.parse(ddl, read="mysql") if ddl else []:
        assert isinstance(stmt, sqlglot.exp.Create)


def test_converter_parses_plans():
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))
    import calcite_plan_to_sql as conv

    plan = """
LogicalProject(EMPNO=[$0], X=[CASE(SEARCH($7, Sarg[[10..20), 30]), 'a', 'b')])
  LogicalFilter(condition=[EXISTS({
LogicalFilter(condition=[=($cor0.DEPTNO, $0)])
  LogicalTableScan(table=[[CATALOG, SALES, DEPT]])
})], variablesSet=[[$cor0]])
    LogicalTableScan(table=[[CATALOG, SALES, EMP]])
"""
    rel = conv.Converter().top(conv.parse_plan(plan))
    assert [t.base for t in rel.types] == ["INTEGER", "CHAR"]
    assert "EXISTS" in rel.sql and "c7 >= 10" in rel.sql
    with pytest.raises(conv.Skip):  # CHAR literals of different lengths are padded by Calcite
        conv.Converter().top(conv.parse_plan("LogicalProject(X=[CASE(=($0, 1), 'a', 'bb')])\n"
                                             "  LogicalTableScan(table=[[CATALOG, SALES, DEPT]])"))


def test_duckdb_runs_a_sample():
    duckdb = pytest.importorskip("duckdb")
    for case in CASES[:25]:
        con = duckdb.connect()
        ddl = SCHEMAS[case["schema_id"]]["ddl"]
        for stmt in sqlglot.transpile(ddl, read="mysql", write="duckdb") if ddl else []:
            con.execute(stmt)
        for key in ("sql_a", "sql_b"):
            con.execute(sqlglot.transpile(case[key], read="mysql", write="duckdb")[0]).fetchall()
