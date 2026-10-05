"""The three SQLSolver pairs the first proof re-check could not run (tools/recheck/calcite_unrunnable.py)."""

from __future__ import annotations

import dataclasses
import sys
from pathlib import Path

import pytest

pytest.importorskip("duckdb")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))

from recheck import engine  # noqa: E402
from recheck.calcite_unrunnable import ADAPTERS  # noqa: E402


def _case(name: str, pair: str):
    adapter = ADAPTERS[name]
    item = next(i for i in adapter.items() if i["pair"] == pair)
    case = adapter.case(item)
    assert case is not None, "the prover no longer proves this pair"
    return case


@pytest.mark.parametrize("name, pair", [("sqlsolver-calcite", "0"), ("sqlsolver-calcite", "193"), ("sqlsolver-spark", "32")])
def test_the_earlier_unrunnable_pairs_run_and_survive(name, pair):
    record = engine.recheck(_case(name, pair), budget=60, seconds=60)
    assert record["verdict"] == "survived", record
    assert record["dbs"] >= 60
    assert "one-side-error" not in record.get("notes", {})


def test_the_subquery_join_pair_is_compared_on_databases_where_both_sides_answer():
    # a negative control: the same pair with the join condition flipped must be separated, so the override runs real answers
    case = _case("sqlsolver-calcite", "193")
    flipped = dataclasses.replace(case, left=case.left.replace("sv.s1 < sv.s2", "sv.s1 > sv.s2"))
    assert flipped.left != case.left
    assert engine.recheck(flipped, budget=800, seconds=60)["verdict"] == "differs"


def test_the_spark_case_pair_negative_control():
    case = _case("sqlsolver-spark", "32")
    changed = dataclasses.replace(case, right=case.right.replace("WHEN TRUE THEN name", "WHEN TRUE THEN 'x'"))
    assert changed.right != case.right
    assert engine.recheck(changed, budget=300, seconds=30)["verdict"] == "differs"


def test_an_override_needs_the_exact_source_text():
    adapter = ADAPTERS["sqlsolver-spark"]
    item = dict(next(i for i in adapter.items() if i["pair"] == "32"))
    item["left"] = item["left"] + " "
    case = adapter.case(item)
    if case is not None:  # a changed pair is translated the plain way (and may not run); never the hand translation
        assert "runnable_fix" not in case.meta
