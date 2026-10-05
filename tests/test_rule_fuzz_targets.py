"""Smoke checks that targeted rule-fuzz templates still reach their intended rewrites."""

import sys
from pathlib import Path

TOOLS = Path(__file__).resolve().parent.parent / "tools"
sys.path.insert(0, str(TOOLS))
import rule_fuzz as fuzz  # noqa: E402
from rule_fuzz_targets.exists_membership import cases as exists_membership_cases  # noqa: E402


def test_exists_membership_targets_reach_rules_and_preserve_near_misses():
    generated = exists_membership_cases(seed=23, count=8)
    results = [fuzz.trace_case(case) for case in generated]
    fired = [{fire.rule for fire in fires} for fires, _ in results]

    assert all(not crashes for _, crashes in results)
    assert "_drop_implied_exists" in fired[0]
    assert "normalize_projected_in" in fired[1]
    assert "normalize_projected_in" in fired[2]
    assert "normalize_projected_in" not in fired[3]
    assert "drop_fk_join" in fired[4]
    assert "drop_fk_join" not in fired[5]
    assert "exists_constant_rules" in fired[6]
    assert "exists_constant_rules" not in fired[7]
