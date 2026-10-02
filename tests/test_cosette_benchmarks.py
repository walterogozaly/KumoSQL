"""Cosette's examples and SPES-only Calcite pairs: floors and zero wrong verdicts (see tools/cosette_bench.py)."""

import importlib.util
import sys
from pathlib import Path

import pytest

pytest.importorskip("z3")
pytest.importorskip("duckdb")

_path = Path(__file__).resolve().parent.parent / "tools" / "cosette_bench.py"
_spec = importlib.util.spec_from_file_location("cosette_bench", _path)
bench = importlib.util.module_from_spec(_spec)
sys.modules["cosette_bench"] = bench
_spec.loader.exec_module(bench)

FLOORS = {"cosette": 55, "spes": 27}


@pytest.mark.parametrize("suite", ["cosette", "spes"])
def test_suite(suite):
    result = bench.run(suite)
    assert result["wrong"] == [], f"wrong verdicts in {suite}: {result['wrong']}"
    assert result["correct"] >= FLOORS[suite], f"{suite}: {result['correct']} correct, floor {FLOORS[suite]}"
