"""Scalar/date/cast target templates still reach their intended rewrites and keep unsafe cases."""

import sys
from pathlib import Path

TOOLS = Path(__file__).resolve().parent.parent / "tools"
sys.path.insert(0, str(TOOLS))
import rule_fuzz as fuzz  # noqa: E402
from rule_fuzz_targets.scalar_folding import cases as scalar_folding_cases  # noqa: E402


def test_scalar_folding_targets_reach_rules_and_preserve_near_misses():
    generated = scalar_folding_cases(seed=31, count=11)
    results = [fuzz.trace_case(case) for case in generated]
    fired = [{fire.rule for fire in fires} for fires, _ in results]

    assert all(not crashes for _, crashes in results)
    assert "fold_literal_int_div" in fired[0]
    assert "fold_literal_int_div" not in fired[1]
    assert "extract_to_ranges" in fired[2]
    assert "extract_to_ranges" not in fired[3]
    assert "_fold_dates" in fired[4]
    assert "_fold_identity_casts" in fired[5]
    assert "fold_casts_and_constant_cases" in fired[6]
    assert "_fold_identity_casts" in fired[7]
    assert "_fold_identity_casts" not in fired[8]
    assert "fold_literal_int_div" not in fired[9]
    assert "_fold_dates" not in fired[10]
