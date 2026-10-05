"""QED's CockroachDB cases (converted to SQL): coverage floor and zero wrong proofs (see tools/qed_cockroach_bench.py)."""

import importlib.util
import os
import sys
from pathlib import Path

import pytest

pytest.importorskip("z3")
pytest.importorskip("duckdb")

_path = Path(__file__).resolve().parent.parent / "tools" / "qed_cockroach_bench.py"
_spec = importlib.util.spec_from_file_location("qed_cockroach_bench", _path)
bench = importlib.util.module_from_spec(_spec)
sys.modules["qed_cockroach_bench"] = bench
_spec.loader.exec_module(bench)

FLOOR = 0
DIFFERENT_GUARDS = frozenset()  # pairs a random database shows different: they must never be proved


def test_qed_cockroach_cases():
    result = bench.run(workers=min(4, os.cpu_count() or 1))
    assert result["wrong"] == [], f"wrong proofs: {result['wrong']}"
    assert result["proved"] >= FLOOR, f"proved {result['proved']}, floor {FLOOR}"
    assert result["scored"] == result["total"] - len(result["different"])
    assert not set(result["verdicts"][n] for n in DIFFERENT_GUARDS) - {"different", "unknown"}
