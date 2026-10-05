"""The bag-equivalence backend (``kumosql.uexpr``) alone on SQLSolver's Calcite, TPC-H and TPC-C pairs.

The rule modules are not involved: this is the backend's own coverage, with a floor a few pairs under the
measured number and zero wrong proofs. Every proof is re-run on random DuckDB databases (20 trials, to stay
fast; ``python tools/uexpr_bench.py`` uses 60). The backend is not wired into the combined prover yet, so the
README scoreboard does not list these numbers (see ``docs/evals/sqlsolver.md``). Calcite is cut into slices so
that no test case runs for more than a minute or so and the slices can run on different workers.
"""

import importlib.util
import sys
from pathlib import Path

import pytest

pytest.importorskip("z3")
pytest.importorskip("duckdb")

_path = Path(__file__).resolve().parent.parent / "tools" / "uexpr_bench.py"
_spec = importlib.util.spec_from_file_location("uexpr_bench", _path)
uexpr_bench = importlib.util.module_from_spec(_spec)
sys.modules["uexpr_bench"] = uexpr_bench
_spec.loader.exec_module(uexpr_bench)
bench = uexpr_bench.bench

TRIALS = 20

# (suite, first pair, one past the last pair, floor). Measured 2026-10-05: calcite 172 of 232, tpch 4 of 22,
# tpcc 19 of 19. A floor only ever goes up.
SLICES = [
    ("calcite", 0, 60, 44),
    ("calcite", 60, 120, 39),
    ("calcite", 120, 180, 43),
    ("calcite", 180, 232, 42),
    ("tpch", 0, 22, 3),
    ("tpcc", 0, 19, 18),
]


def _only(first: int, last: int):
    """The backend on pairs ``first`` to ``last - 1`` of a suite (the harness calls ``prove`` once per pair, in order)."""

    calls = [-1]

    def prove(left, right, tables, constants=False):
        calls[0] += 1
        if not first <= calls[0] < last:
            return False
        return uexpr_bench.prove(left, right, tables, constants)

    return prove


@pytest.mark.parametrize(
    "suite,first,last,floor",
    [pytest.param(*row, id=f"{row[0]}-{row[1]}-{row[2]}") for row in SLICES],
)
def test_backend_alone_on_sqlsolver_suites(suite, first, last, floor):
    result = bench.run_suite(suite, _only(first, last), trials=TRIALS)
    assert result.wrong == [], f"wrong proofs in {suite}: {[i for i, _ in result.wrong]}"
    assert result.proved >= floor, f"{suite}[{first}:{last}]: proved {result.proved}, floor {floor}"
