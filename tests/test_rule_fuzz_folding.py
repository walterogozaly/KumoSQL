"""The folding template module of the rule fuzzer (tools/rule_fuzz_targets/folding.py) still makes each rule it targets fire."""

import sys
from pathlib import Path

import pytest

pytest.importorskip("z3")

TOOLS = Path(__file__).resolve().parent.parent / "tools"
sys.path.insert(0, str(TOOLS))
import rule_fuzz as fuzz  # noqa: E402
from rule_fuzz_targets import folding  # noqa: E402

TARGETS = {
    "_bigquery_sugar",
    "_fold_boolean_constants",
    "_fold_constants",
    "_fold_dates",
    "_fold_identity_casts",
    "_fold_null_guards",
    "_fold_trivia",
    "_lowercase_columns",
    "canonical_negation",
    "drop_subsumed_like",
    "extract_to_ranges",
    "fold_casts_and_constant_cases",
    "fold_literal_int_div",
    "fold_string_literals",
}


def test_every_target_rule_fires_on_the_first_pass_of_the_templates():
    cases = folding.cases(1, len(folding.TEMPLATES))
    fired = set()
    for case in cases:
        fires, _ = fuzz.trace_case(case, only=TARGETS)
        fired.update(fire.rule for fire in fires)
    assert TARGETS <= fired, f"no longer fire: {sorted(TARGETS - fired)}"
