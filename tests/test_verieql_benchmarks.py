"""VeriEQL benchmark floors on small, evenly spaced samples (the suites are downloaded and cached).

``FLOORS`` only ever goes up: a drop means a regression in proving or counterexample power. A wrong
verdict (a proof contradicted by a counterexample) fails the test whatever the floors say.
"""

import importlib.util
import sys
from pathlib import Path

import pytest

pytest.importorskip("z3")
pytest.importorskip("duckdb")

_path = Path(__file__).resolve().parent.parent / "tools" / "verieql_bench.py"
_spec = importlib.util.spec_from_file_location("verieql_bench", _path)
bench = importlib.util.module_from_spec(_spec)
sys.modules["verieql_bench"] = bench
_spec.loader.exec_module(bench)

# suite -> (every Nth case, minimum equivalent proofs, minimum executed counterexamples)
SAMPLES = {
    "literature": (4, 2, 4),
    "calcite": (20, 7, 0),
    "leetcode": (200, 19, 21),
}


@pytest.mark.parametrize("suite", sorted(SAMPLES))
def test_verieql_sample(suite):
    every, proofs, refutations = SAMPLES[suite]
    try:
        result = bench.run_suite(suite, every=every, jobs=1, audit=True)
    except bench.DataUnavailable as error:  # no network and nothing cached, or a download that kept failing
        pytest.skip(str(error))
    assert bench.wrong_count(result) == 0, f"wrong verdicts in {suite}: {result.audit_cases}"
    assert result.counts[bench.EQUIVALENT] >= proofs
    assert result.counts[bench.DIFFERENT] >= refutations
