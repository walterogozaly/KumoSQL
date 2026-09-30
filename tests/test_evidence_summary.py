import json

import pytest

from kumosql import (
    RewriteResult,
    Verification,
    VerificationCheck,
    VerificationStatus,
    apply_rule,
    summarize_evidence,
)

S = VerificationStatus


def make(status, before="select 1", after="select 2", planner=None, synthetic=None):
    checks = (VerificationCheck("planner", planner, "SECRET detail"),) if planner else ()
    if synthetic:
        checks += (VerificationCheck("synthetic_results", synthetic, "SECRET rows"),)
    return RewriteResult(
        "r", before, after, 1, (), Verification(status, "SECRET reason", ("SECRET",), checks), True
    )


def mixed():
    return [
        make(S.UNCHANGED, after="select 1"),
        make(S.PROVEN, planner="passed"),
        make(S.PROVEN),
        make(S.PLANNER_CHECKED, planner="passed"),
        make(S.UNPROVEN, planner="failed"),
        make(S.FAILED),
    ]


def test_mixed_percentages_and_disjoint_buckets():
    s = summarize_evidence(mixed(), min_changed=1)
    assert (s.total, s.unchanged, s.changed) == (6, 1, 5)
    assert s.proven + s.planner_checked + s.unproven + s.failed == s.changed
    assert s.pct_useful_evidence == s.pct_proof == 40.0
    assert s.pct_planner_only == 20.0
    assert s.pct_no_evidence == 40.0
    assert (s.planner_passed, s.planner_failed) == (2, 1)
    assert s.pct_planner_passed == 40.0


def test_zero_changed_and_small_denominator_suppressed():
    assert summarize_evidence([], min_changed=1).pct_proof is None
    only_same = summarize_evidence([make(S.UNCHANGED, after="select 1")], min_changed=1)
    assert only_same.pct_useful_evidence is None
    assert summarize_evidence(mixed()[:5]).pct_useful_evidence is None


def test_gate_violations_raise():
    with pytest.raises(ValueError):
        summarize_evidence([make(S.UNCHANGED)])
    with pytest.raises(ValueError):
        summarize_evidence([make(S.PROVEN, after="select 1")])


def test_real_rewrite_results():
    sql = "SELECT a FROM t WHERE TRUE AND a > 1"
    results = [
        apply_rule("remove_trivial_predicates", sql),
        apply_rule("remove_trivial_predicates", "SELECT 1"),
    ]
    s = summarize_evidence(results, min_changed=1)
    assert s.changed == 1 and s.unchanged == 1
    assert s.proven == 1 and s.pct_useful_evidence == 100.0


def test_serialized_aggregate_is_anonymous():
    text = json.dumps(summarize_evidence(mixed(), min_changed=1).to_json())
    assert "SECRET" not in text and "select" not in text.lower()
    assert set(json.loads(text)) == {
        "total", "unchanged", "changed", "labels", "planner", "useful_evidence",
        "synthetic_agreed", "percent_of_changed",
    }


def test_synthetic_agreement_counts_toward_useful_evidence_once():
    results = [
        make(S.PROVEN, synthetic="passed"),  # both: counted once
        make(S.UNPROVEN, synthetic="passed"),  # agreement only
        make(S.PLANNER_CHECKED, planner="passed", synthetic="passed"),
        make(S.UNPROVEN, synthetic="failed"),  # disagreement is not evidence
        make(S.UNPROVEN, synthetic="inconclusive"),
        make(S.FAILED, synthetic="passed"),  # failed result never counts
        make(S.PROVEN),
        make(S.UNPROVEN),
    ]
    s = summarize_evidence(results, min_changed=1)
    assert s.changed == 8
    assert s.proven == 2 and s.synthetic_agreed == 3 and s.useful_evidence == 4
    assert s.pct_useful_evidence == 50.0
    assert s.pct_proof == 25.0  # proof stays separate from the headline
    assert s.pct_synthetic_agreed == 37.5
    assert s.pct_planner_only == 12.5
    assert s.to_json()["useful_evidence"] == 4
