"""Checks for the QED CockroachDB fixture produced by tools/qed_cockroach_to_sql.py."""

import importlib.util
import json
import sys
from pathlib import Path

import pytest
import sqlglot

_tools = Path(__file__).resolve().parent.parent / "tools"
sys.path.insert(0, str(_tools))
_spec = importlib.util.spec_from_file_location("qed_cockroach_to_sql", _tools / "qed_cockroach_to_sql.py")
conv = importlib.util.module_from_spec(_spec)
sys.modules["qed_cockroach_to_sql"] = conv
_spec.loader.exec_module(conv)

FIXTURES = Path(__file__).parent / "fixtures" / "qed_cockroach"
CASES = [json.loads(line) for line in (FIXTURES / "qed_cockroach_pairs.jsonl").read_text().splitlines()]
SKIPPED = [json.loads(line) for line in (FIXTURES / "qed_cockroach_skipped.jsonl").read_text().splitlines()]


def test_fixture_loads():
    assert len(CASES) > 700
    assert len({c["name"] for c in CASES}) == len(CASES)
    for c in CASES:
        assert {"name", "sql_a", "sql_b", "ddl", "schema_id", "schemas"} <= set(c)
        # a few pairs only use VALUES and read no table
        assert c["ddl"].startswith("CREATE TABLE") == bool(c["schemas"])
        assert c["name"].split("/")[0] in ("memo", "norm", "xform")


def test_summary_matches_fixture():
    summary = json.loads((FIXTURES / "summary.json").read_text())
    assert summary["source_commit"] == "9e9c2621d6d922007694a72f9cc2d5ed0de2eccd"
    assert summary["converted"] == len(CASES)
    assert summary["skipped"] == len(SKIPPED)
    assert summary["files"] == len(CASES) + len(SKIPPED) == 1287
    assert all(s["reason"] for s in SKIPPED)
    assert not {c["name"] for c in CASES} & {s["name"] for s in SKIPPED}


def test_licence_and_provenance_are_recorded():
    assert (FIXTURES / "LICENSE").read_text().startswith("MIT License")
    readme = (FIXTURES / "README.md").read_text()
    for text in ("9e9c262", "v23.1.3", "Business Source License"):
        assert text in readme


@pytest.mark.parametrize("case", CASES, ids=[c["name"] for c in CASES])
def test_pair_parses(case):
    for key in ("sql_a", "sql_b"):
        tree = sqlglot.parse_one(case[key], read="mysql")
        assert isinstance(tree, sqlglot.exp.Query)
    for stmt in sqlglot.parse(case["ddl"], read="mysql"):
        assert isinstance(stmt, sqlglot.exp.Create)


def test_duckdb_runs_a_sample():
    duckdb = pytest.importorskip("duckdb")
    for case in CASES[::20]:
        con = duckdb.connect()
        for stmt in sqlglot.transpile(case["ddl"], read="mysql", write="duckdb"):
            con.execute(stmt)
        for key in ("sql_a", "sql_b"):
            con.execute(sqlglot.transpile(case[key], read="mysql", write="duckdb")[0]).fetchall()


# ---------------------------------------------------------------- the converter's rules
def scan_doc(types=("INT", "INT", "OID"), queries=None, help_=("",)):
    return {
        "help": list(help_),
        "schemas": [{"key": [[0]], "nullable": [False, True, True], "types": list(types)}],
        "queries": queries,
    }


def col(i, t="INT"):
    return {"column": i, "type": t}


def lit(v, t="INT"):
    return {"operand": [], "operator": v, "type": t}


def op(name, *operands, t="BOOL"):
    return {"operand": list(operands), "operator": name, "type": t}


def filt(cond, source=None):
    return {"filter": {"condition": op("AND", cond, t="BOOLEAN"), "source": source or {"scan": 0}}}


def project(target, source=None):
    return {"project": {"source": source or {"scan": 0}, "target": target}}


def convert(*queries, **kw):
    return conv.convert_case(scan_doc(queries=list(queries), **kw))


def skipped(*queries, **kw):
    with pytest.raises(conv.Skip) as e:
        convert(*queries, **kw)
    return str(e.value)


def test_operator_spellings():
    q = project([col(0)], filt(op("<=", col(1), lit("5"), t="BOOLEAN")))
    out = convert(q, q)
    assert "<= 5" in out["sql_a"]
    q = project([col(0)], filt(op("IS NOT", col(1), lit("5"))))
    assert "(NOT (t1.c1 <=> 5))" in convert(q, q)["sql_a"]
    q = project([col(0)], filt(op("<=>", col(1), col(0), t="BOOLEAN")))
    assert "(t1.c1 <=> t1.c0)" in convert(q, q)["sql_a"]
    q = project([col(0)], filt(op("IS", col(1), lit("NULL"))))
    assert "<=> NULL" in convert(q, q)["sql_a"]


def test_conjunct_lists_and_case_input():
    assert "WHERE TRUE" in convert(project([col(0)], filt(op("AND", t="BOOLEAN"))), project([col(0)]))["sql_a"]
    case = op("CASE", lit("TRUE", "BOOL"), op("EQ", col(0), lit("1")), col(1), lit("NULL"), t="INT")
    sql = convert(project([case]), project([col(0)]))["sql_a"]
    assert "CASE TRUE WHEN (t1.c0 = 1) THEN t1.c1 ELSE NULL END" in sql


def test_opaque_columns_pass_through_but_are_never_read():
    # column 2 is an OID: it may sit in the table and be dropped by a projection ...
    assert convert(project([col(0)]), project([col(0)]))["ddl"].count("BIGINT") >= 3
    # ... but not be compared, returned, grouped on a non-equality type or sorted on
    assert "OID" in skipped(project([col(0)], filt(op("EQ", col(2, "INT"), lit("1")))), project([col(0)]))
    assert "(no exact encoding)" in skipped({"scan": 0}, {"scan": 0}, types=("INT", "INT", "GEOMETRY"))
    sort = {"sort": {"collation": [[2, "INT", "ASCENDING"]], "source": {"scan": 0}, "limit": lit("1")}}
    assert "ORDER BY" in skipped(project([col(0)], sort), project([col(0)]))
    # an opaque type with plain equality may be returned
    assert convert({"scan": 0}, {"scan": 0})["sql_a"]


def test_unmodelled_things_are_skipped_with_a_reason():
    assert "FUNCTION" in skipped(project([op("FUNCTION", op("SCALAR LIST", col(0), t="ANYELEMENT"), t="INT")]), project([col(0)]))
    assert "LIMIT/OFFSET without ORDER BY" in skipped(
        {"sort": {"collation": [], "source": {"scan": 0}, "limit": lit("1")}}, project([col(0)]))
    assert "CHECK" in skipped(project([col(0)]), project([col(0)]), help_=("check constraint expressions",))
    assert "PLACEHOLDER" in skipped(project([col(0)], filt(op("EQ", col(0), lit("PLACEHOLDER")))), project([col(0)]))
    assert "unsupported operator DIV" in skipped(project([op("DIV", col(0), col(1), t="DECIMAL")]), project([col(0)]))
    assert "CONST AGG" in skipped(
        {"group": {"function": [{"operator": "CONST AGG", "operand": [col(1)], "type": "INT", "distinct": False, "ignoreNulls": False}],
                   "keys": [col(0)], "source": {"scan": 0}}}, project([col(0)]))


def test_values_with_no_columns_is_the_one_empty_row():
    empty = {"values": {"content": [[]], "schema": []}}
    out = convert(project([lit("1")], empty), project([lit("1")], empty))
    assert "(SELECT 1 AS z)" in out["sql_a"]
    assert out["ddl"] == "" and out["schemas"] == []


def test_set_operations_and_distinct():
    both = {"union": [project([col(0)]), project([col(0)])]}
    assert "UNION ALL" in convert(both, both)["sql_a"]
    assert "SELECT DISTINCT" in convert({"distinct": both}, both)["sql_a"]
    assert "EXCEPT" in convert({"except": [project([col(0)]), project([col(1)])]}, both)["sql_a"]
