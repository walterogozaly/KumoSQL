"""SQLSolver's published benchmark pairs: coverage floors and zero wrong proofs.

Every proof is re-checked by running both queries on random DuckDB databases that
respect the schema's constraints. ``FLOORS`` only ever goes up: a drop means a
regression in proving power, a wrong proof means a soundness bug.
"""

import importlib.util
import sys
from pathlib import Path

import pytest

pytest.importorskip("z3")
pytest.importorskip("duckdb")

_path = Path(__file__).resolve().parent.parent / "tools" / "sqlsolver_bench.py"
_spec = importlib.util.spec_from_file_location("sqlsolver_bench", _path)
bench = importlib.util.module_from_spec(_spec)
sys.modules["sqlsolver_bench"] = bench
_spec.loader.exec_module(bench)

FLOORS = {"calcite": 160, "spark": 105, "tpch": 19, "tpcc": 19}


@pytest.mark.parametrize(
    "suite",
    [
        "calcite",
        "spark",
        "tpcc",
        pytest.param("tpch", marks=pytest.mark.slow),
    ],
)
def test_benchmark_suite(suite):
    result = bench.run_suite(suite, bench.default_prove)
    assert result.wrong == [], f"wrong proofs in {suite}: {[i for i, _ in result.wrong]}"
    assert result.proved >= FLOORS[suite], f"{suite}: proved {result.proved}, floor {FLOORS[suite]}"
