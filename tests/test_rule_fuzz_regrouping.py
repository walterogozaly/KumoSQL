"""The targeted corpus must exercise regrouping rules, not just generate SQL."""

import sys
from pathlib import Path

import pytest

pytest.importorskip("z3")
pytest.importorskip("duckdb")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))
import rule_fuzz as fuzz
from rule_fuzz_targets import regrouping


RULES = {"_collapse_aggregate", "regroup_arithmetic", "_roll_up_aggregate", "_regroup_distinct", "distinct_rules"}


def test_targeted_regrouping_corpus_checks_each_rule():
    checked = set()
    for case in regrouping.cases(seed=496, count=len(regrouping.TEMPLATES)):
        result = fuzz.check_case(case, only=RULES, seed=496)
        # GROUPING SETS is deliberately a guard case. Earlier firings remain checkable
        # when a later normalization declines to render it faithfully.
        assert all(crash.startswith("LossySql:") for crash in result["crashes"])
        assert not [fire for fire in result["fires"] if fire["status"] == "differs"]
        checked.update(fire["rule"] for fire in result["fires"] if fire["status"] == "equal")
    assert {"_collapse_aggregate", "regroup_arithmetic", "distinct_rules"} <= checked
