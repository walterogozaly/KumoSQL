"""The guards template module of the rule fuzzer (tools/rule_fuzz_targets/guards.py) still makes each rule it targets fire."""

import sys
from pathlib import Path

import pytest

pytest.importorskip("z3")

TOOLS = Path(__file__).resolve().parent.parent / "tools"
sys.path.insert(0, str(TOOLS))
import rule_fuzz as fuzz  # noqa: E402
from rule_fuzz_targets import guards  # noqa: E402

TARGETS = {
    "_distribute",
    "_drop_derived_null_guard",
    "_drop_global_null_filter",
    "_inline_constant_columns",
    "_inline_constant_source",
    "_probe_and_nth_value",
    "_single_row_source",
    "_values_to_union",
    "distribute_over_constant_union",
    "expand_alias_columns",
    "parenthesize_is_operands",
}


def test_every_target_rule_fires_on_the_first_pass_of_the_templates():
    cases = guards.cases(1, len(guards.TEMPLATES))
    fired = set()
    for case in cases:
        fires, _ = fuzz.trace_case(case, only=TARGETS)
        fired.update(fire.rule for fire in fires)
    assert TARGETS <= fired, f"no longer fire: {sorted(TARGETS - fired)}"
