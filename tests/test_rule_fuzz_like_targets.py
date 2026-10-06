"""Reach and guard checks for the subsumed-LIKE rule-fuzz target."""

import sys
from pathlib import Path

import pytest

pytest.importorskip("z3")
pytest.importorskip("duckdb")

TOOLS = Path(__file__).resolve().parent.parent / "tools"
sys.path.insert(0, str(TOOLS))
import rule_fuzz as fuzz  # noqa: E402


def test_like_target_reaches_prefix_cases_and_preserves_guards():
    cases = fuzz.target_cases("like_patterns", seed=31, count=12)
    positive = {0, 1, 2, 3, 4, 11}

    assert len(cases) == 12
    for index, case in enumerate(cases):
        fires, crashes = fuzz.trace_case(case, only={"drop_subsumed_like"})
        assert not crashes, (index, crashes)
        if index in positive:
            assert any(fire.rule == "drop_subsumed_like" for fire in fires), (index, case["sql"])
        else:
            assert not fires, (index, case["sql"], [fire.rule for fire in fires])
