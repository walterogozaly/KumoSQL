"""Derived/structural templates continue to exercise their intended rewrites."""

import sys
from pathlib import Path

TOOLS = Path(__file__).resolve().parent.parent / "tools"
sys.path.insert(0, str(TOOLS))
import rule_fuzz as fuzz  # noqa: E402
from rule_fuzz_targets.derived_structural import cases as derived_structural_cases  # noqa: E402


def test_derived_structural_targets_reach_rules_and_keep_boundaries():
    generated = derived_structural_cases(seed=29, count=8)
    results = [fuzz.trace_case(case) for case in generated]
    fired = [{fire.rule for fire in fires} for fires, _ in results]

    assert all(not crashes for _, crashes in results)
    assert "outer_join_rules" in fired[0]
    assert "_inline_expression_projection" in fired[2]
    assert "_inline_expression_projection" in fired[3]
    assert "_merge_spj_source" in fired[4]
    assert "_flatten_join_source" in fired[5]
    assert "_merge_spj_source" not in fired[6]
    assert "_flatten_join_source" not in fired[6]
