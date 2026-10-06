"""Reachability and guard coverage for ``tools/rule_fuzz_targets/limits.py``."""

import sys
from pathlib import Path

_TOOLS = Path(__file__).resolve().parent.parent / "tools"
sys.path.insert(0, str(_TOOLS))

import rule_fuzz as fuzz  # noqa: E402
from rule_fuzz_targets import limits  # noqa: E402


POSITIVE = {0, 1, 2, 3, 4}
GUARDED = {5, 6, 7, 8, 9, 10}


def _target_fire(case):
    fires, crashes = fuzz.trace_case(case, only={"limit_rule"})
    assert not crashes
    return any(fire.rule == "limit_rule" for fire in fires)


def test_limit_target_reaches_each_rewrite_shape():
    cases = limits.cases(seed=496, count=len(limits.TEMPLATES))
    assert all(_target_fire(cases[index]) for index in POSITIVE)


def test_limit_target_keeps_order_and_cut_guards():
    cases = limits.cases(seed=496, count=len(limits.TEMPLATES))
    assert not any(_target_fire(cases[index]) for index in GUARDED)
