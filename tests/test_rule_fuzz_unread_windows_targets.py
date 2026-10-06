"""Reachability and guard coverage for ``tools/rule_fuzz_targets/unread_windows.py``."""

import sys
from pathlib import Path

_TOOLS = Path(__file__).resolve().parent.parent / "tools"
sys.path.insert(0, str(_TOOLS))

import rule_fuzz as fuzz  # noqa: E402
from rule_fuzz_targets import unread_windows  # noqa: E402


POSITIVE = {0, 1, 2, 3, 4}
GUARDED = set(range(5, len(unread_windows.TEMPLATES)))


def _target_fire(case):
    fires, crashes = fuzz.trace_case(case, only={"unread_windows"})
    assert not crashes
    return any(fire.rule == "drop_unread_windows" for fire in fires)


def test_unread_window_target_reaches_each_rewrite_shape():
    cases = unread_windows.cases(seed=496, count=len(unread_windows.TEMPLATES))
    assert all(_target_fire(cases[index]) for index in POSITIVE)


def test_unread_window_target_preserves_values_that_can_be_read():
    cases = unread_windows.cases(seed=496, count=len(unread_windows.TEMPLATES))
    assert not any(_target_fire(cases[index]) for index in GUARDED)
