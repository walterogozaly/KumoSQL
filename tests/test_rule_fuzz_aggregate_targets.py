"""The aggregate/count target templates continue to reach their intended rules."""

import sys
from pathlib import Path

TOOLS = Path(__file__).resolve().parent.parent / "tools"
sys.path.insert(0, str(TOOLS))
import rule_fuzz as fuzz  # noqa: E402
from rule_fuzz_targets.aggregate_counts import cases as aggregate_count_cases  # noqa: E402


def test_aggregate_count_targets_reach_rules_and_preserve_near_misses():
    generated = aggregate_count_cases(seed=19, count=10)
    results = [fuzz.trace_case(case) for case in generated]
    fired = [{fire.rule for fire in fires} for fires, _ in results]

    assert all(not crashes for _, crashes in results)
    assert "_constant_counts" in fired[0]
    assert "fold_grouped_count_cases" in fired[1]
    assert "fold_grouped_count_cases" not in fired[2]
    assert "sum_of_grouped_counts" in fired[3]
    assert "sum_of_grouped_counts" not in fired[4]
    assert "singleton_count_sum" in fired[5]
    assert "singleton_count_sum" not in fired[6]
    assert "regroup_tuple_count" in fired[7]
    assert "regroup_tuple_count" not in fired[8]
    assert "_constant_counts" not in fired[9]
