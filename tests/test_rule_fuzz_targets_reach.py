"""The targeted rule-fuzz generators still reach the rules they are aimed at.

A template stops firing when an earlier rewrite starts folding its shape first (``_roll_up_aggregate`` was dropped
for that reason), or when a rewrite of the template language leaves it unparseable. The check traces ``normalize`` on
a few cases per module without running DuckDB and asserts that every target rule fired at least once.
"""

import sys
from pathlib import Path

import pytest

pytest.importorskip("z3")

TOOLS = Path(__file__).resolve().parent.parent / "tools"
sys.path.insert(0, str(TOOLS))
import rule_fuzz as fuzz  # noqa: E402

TARGETS = {
    "regroup": {"_collapse_aggregate", "regroup_arithmetic"},
    "partitions": {"recombine_partitions"},
    "keyed_sets": {"lift_keyed_set_join"},
    "intersections": {"collapse_counted_intersection", "collapse_named_counted_intersection"},
    "constant_correlations": {"propagate_constant_correlations"},
}


@pytest.mark.parametrize("module", sorted(TARGETS))
def test_module_reaches_its_rules(module):
    cases = fuzz.target_cases(module, seed=1, count=40)
    fired = set()
    crashes = []
    for case in cases:
        fires, errors = fuzz.trace_case(case)
        fired.update(fire.rule for fire in fires)
        crashes.extend(errors)
    assert TARGETS[module] <= fired, f"{module}: never fired {sorted(TARGETS[module] - fired)}"
    assert not [c for c in crashes if "parse" in c.lower()], crashes[:3]
