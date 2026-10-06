"""The outer-join target generator must exercise duplicate-free grouping and anti-join null extension."""

import sys
from pathlib import Path

import pytest

pytest.importorskip("z3")
pytest.importorskip("duckdb")

TOOLS = Path(__file__).resolve().parent.parent / "tools"
sys.path.insert(0, str(TOOLS))
import rule_fuzz as fuzz  # noqa: E402
from rule_fuzz_targets import outer_joins  # noqa: E402


@pytest.mark.parametrize(
    ("offset", "should_fire"),
    [(-4, True), (-3, False), (-2, True), (-1, False)],
    ids=["grouped-outer-join", "hidden-group-key", "anti-null-extends", "narrow-anti-test"],
)
def test_outer_join_targeted_cases_reach_the_rule_and_preserve_guards(offset, should_fire):
    cases = outer_joins.cases(seed=518, count=len(outer_joins.TEMPLATES))
    result = fuzz.check_case(cases[offset], seed=518, only={"grouped_outer_join_rules"})
    fires = [fire for fire in result["fires"] if fire["rule"] == "grouped_outer_join_rules"]

    assert bool(fires) is should_fire
    assert all(fire["status"] == "equal" for fire in fires), fires
