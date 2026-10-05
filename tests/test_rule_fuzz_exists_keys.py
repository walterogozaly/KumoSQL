"""The EXISTS, membership and key generators (tools/rule_fuzz_targets/exists_keys.py and membership.py) still make the
rules they target fire. Only the trace runs, not DuckDB: a template that stops firing is caught here cheaply."""

import sys
from pathlib import Path

import pytest

pytest.importorskip("z3")
pytest.importorskip("duckdb")

TOOLS = Path(__file__).resolve().parent.parent / "tools"
sys.path.insert(0, str(TOOLS))
import rule_fuzz as fuzz  # noqa: E402
from rule_fuzz_targets import exists_keys, membership  # noqa: E402

TARGETS = {
    "exists_keys": (
        exists_keys,
        {
            "drop_fk_join",
            "exists_constant_rules",
            "expose_correlated_key_groups",
            "drop_keyed_distinct",
            "remove_keyed_grouping",
            "exists_over_aggregate",
            "keyed_join_to_exists",
            "_drop_exists_witnessed_by_join",
        },
    ),
    "membership": (
        membership,
        {
            "_select_list_in_to_exists",
            "normalize_projected_in",
            "_drop_group_in_membership_tests",
            "_grouped_in_to_derived",
            "_in_over_union",
            "rewrite_quantified",
            "_semi_joins_to_exists",
            "_pull_up_exists",
            "_drop_implied_exists",
            "nullable_lateral_boolean_group",
        },
    ),
}


@pytest.mark.parametrize("name", sorted(TARGETS))
def test_every_target_rule_fires(name):
    module, targets = TARGETS[name]
    known = set(fuzz.rule_names())
    assert targets <= known, targets - known
    fired = set()
    for case in module.cases(1, 3 * len(module.TEMPLATES)):
        fires, _ = fuzz.trace_case(case, only=targets)
        fired |= {fire.rule for fire in fires}
        if targets <= fired:
            break
    assert targets <= fired, sorted(targets - fired)


@pytest.mark.parametrize("name", sorted(TARGETS))
def test_cases_are_reproducible_and_legal(name):
    module, _ = TARGETS[name]
    first = module.cases(7, 40)
    assert first == module.cases(7, 40)
    for case in first:
        assert set(case["constraints"]) <= set(case["schema"])
        for table, facts in case["constraints"].items():
            columns = {c for c, _ in case["schema"][table]}
            for key in facts.get("keys", []):
                assert set(key) <= columns
            for child, parent, parent_cols in facts.get("foreign_keys", []):
                assert set(child) <= columns and set(parent_cols) <= {c for c, _ in case["schema"][parent]}
