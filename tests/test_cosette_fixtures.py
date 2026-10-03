"""The Cosette and SPES-only fixtures load, parse and run."""

from __future__ import annotations

import json
from pathlib import Path
import re

import pytest
import sqlglot

FIXTURES = Path(__file__).resolve().parent / "fixtures"
COSETTE = FIXTURES / "cosette"
SPES = FIXTURES / "spes"


def _jsonl(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


COSETTE_CASES = _jsonl(COSETTE / "cosette_cases.jsonl")
SPES_PAIRS = _jsonl(SPES / "spes_only_pairs.jsonl")


def _parses(sql: str) -> bool:
    try:
        sqlglot.parse_one(sql, read="mysql")
    except sqlglot.errors.ParseError:
        return False
    return True


# sqlglot 26 cannot parse a qualified column named CASE (one SPES pair has t2.CASE)
QUALIFIED_CASE = _parses("SELECT t2.CASE FROM t AS t2")


def test_counts_match_summaries():
    cos = json.loads((COSETTE / "summary.json").read_text())
    assert cos["converted"] == len(COSETTE_CASES)
    assert cos["skipped"] == len(_jsonl(COSETTE / "cosette_skipped.jsonl"))
    spes = json.loads((SPES / "summary.json").read_text())
    assert spes["kept"] == len(SPES_PAIRS)
    assert spes["skipped"] == len(_jsonl(SPES / "spes_only_skipped.jsonl"))
    for path in (COSETTE / "cosette_skipped.jsonl", SPES / "spes_only_skipped.jsonl"):
        assert all(row["reason"] and row["category"] != "other" for row in _jsonl(path))


@pytest.mark.parametrize("case", COSETTE_CASES, ids=lambda c: f"{c['source_dir']}/{c['name']}")
def test_cosette_case_parses(case):
    assert case["label"] in {"equivalent", "not_equivalent", "conditional"}
    assert bool(case["constraints"]) == (case["label"] == "conditional")
    for sql in (case["sql_a"], case["sql_b"]):
        assert sqlglot.parse_one(sql, read="mysql") is not None
    tables = {t["name"].lower(): t for t in case["schema"]["tables"]}
    for stmt in sqlglot.parse(case["ddl"], read="mysql"):
        assert stmt.this.this.name.lower() in tables
    for k in case["constraints"]:
        cols = {c["name"] for c in tables[k["table"]]["columns"]}
        assert set(k["columns"]) <= cols
    for pred in case.get("predicates", {}):
        assert re.search(rf"\.__{pred}\b", case["sql_a"] + case["sql_b"])


@pytest.mark.parametrize("pair", SPES_PAIRS, ids=lambda p: f"{p['spes_index']}-{p['name']}")
def test_spes_pair_parses(pair):
    assert pair["label"] == "equivalent" or (pair["label"] == "not_equivalent" and pair.get("label_note"))
    for sql in (pair["sql_a"], pair["sql_b"]):
        if not QUALIFIED_CASE and re.search(r"\.CASE\b", sql):
            pytest.skip("this sqlglot version cannot parse a qualified column named CASE")
        assert sqlglot.parse_one(sql, read="mysql") is not None


def test_cosette_cases_run_in_duckdb():
    duckdb = pytest.importorskip("duckdb")
    for case in COSETTE_CASES:
        con = duckdb.connect()
        con.execute(case["ddl"])
        for sql in (case["sql_a"], case["sql_b"]):
            con.execute(re.sub(r'(?<![\w"])(\$\w+)', r'"\1"', sql)).fetchall()
        con.close()


def test_overlap_inventory():
    report = json.loads((FIXTURES / "calcite_overlap.json").read_text())
    assert report["unique_names_all_corpora"] == len(report["tests"])
    for corpus, count in report["unique_names"].items():
        assert count == sum(corpus in v for v in report["tests"].values())
    cosette_calcite = {c["name"] for c in COSETTE_CASES if c["source_dir"] == "calcite"}
    assert cosette_calcite == {n for n, v in report["tests"].items() if "cosette" in v}
    assert {p["name"] for p in SPES_PAIRS} == {
        n for n, v in report["tests"].items() if "spes_only" in v}
