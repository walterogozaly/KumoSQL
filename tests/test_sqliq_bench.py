"""SQL-IQ harness: the judge's decisions on small hand-written pairs (no SQL-IQ checkout needed)."""

import importlib.util
import sys
from pathlib import Path

import pytest

pytest.importorskip("z3")

_path = Path(__file__).resolve().parent.parent / "tools" / "sqliq_bench.py"
_spec = importlib.util.spec_from_file_location("sqliq_bench", _path)
bench = importlib.util.module_from_spec(_spec)
sys.modules["sqliq_bench"] = bench
_spec.loader.exec_module(bench)

TABLES = {"t": {"id": "INTEGER", "a": "INTEGER", "name": "TEXT"}, "u": {"id": "INTEGER", "t_id": "INTEGER"}}


def pair(sql1, sql2, label="yes", keys=None):
    return bench.Pair(0, sql1, sql2, TABLES, keys or {"t": ("id",)}, label)


def test_a_rewrite_is_proved():
    answer, how = bench.judge(pair("SELECT a FROM t WHERE a > 1 AND a < 5", "SELECT a FROM t WHERE a < 5 AND a > 1"))
    assert (answer, how) == ("yes", "proved")


def test_a_changed_literal_is_found_by_testing():
    answer, how = bench.judge(pair("SELECT id FROM t WHERE name = 'x'", "SELECT id FROM t WHERE name = 'X'", "no"))
    assert answer == "no"
    assert how == "differs"


def test_output_names_do_not_matter():
    assert bench.judge(pair("SELECT a AS x FROM t", "SELECT a AS y FROM t"))[0] == "yes"


def test_unparseable_sql_is_answered_no():
    assert bench.judge(pair("SELECT nope FROM missing", "SELECT a FROM t", "no"))[0] == "no"


def test_score_reports_both_classes():
    pairs = [pair("SELECT 1", "SELECT 1", "yes"), pair("SELECT 1", "SELECT 2", "no")]
    metrics = bench.score(pairs, [("yes", "proved"), ("yes", "tested")])
    assert metrics["correct"] == 1
    assert metrics["equivalent_accuracy"] == 1.0
    assert metrics["non_equivalent_accuracy"] == 0.0
    assert metrics["geometric_mean"] == 0.0
