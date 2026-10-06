from __future__ import annotations

from types import SimpleNamespace

import pytest

from kumosql.joinorder.runtime_model import Pair, cross_validate, fit_ratio, plan_work
from kumosql.joinorder.view_cost import BIG_BUILD_ROWS, hash_join_work


def _pair(query: str, group: str, after: float) -> Pair:
    return Pair(query, group, 10.0, after, {"scan": 1.0}, {"scan": 1.0 + int(query[1:])})


def test_cross_validation_prediction_does_not_depend_on_its_held_out_group():
    pairs = [_pair(f"q{i}", f"v{i}", 1.0 + i) for i in range(6)]
    changed = [*pairs]
    changed[0] = _pair("q0", "v0", 100.0)

    before = cross_validate(pairs, ("scan",))
    after = cross_validate(changed, ("scan",))

    assert before[0] == after[0]
    assert before[1:] != after[1:]


def test_cross_validation_requires_distinct_groups():
    with pytest.raises(ValueError, match="at least two distinct groups"):
        cross_validate([_pair("q0", "v0", 1.0)], ("scan",))


def test_ratio_model_predicts_a_clipped_log_ratio():
    pairs = [
        Pair(f"q{i}", f"v{i}", 10.0, 10.0 * (i + 1), {"scan": 0.0}, {"scan": float(i + 1)})
        for i in range(5)
    ]
    model = fit_ratio(pairs, ("scan",), l2=0.0)

    assert model.log_ratio({"scan": 0.0}, {"scan": 1_000_000.0}) == pytest.approx(5.0)


def test_plan_work_counts_scan_join_and_build_probe_rows():
    plan = {
        "type": "HASH_JOIN",
        "est": 30,
        "act": 31,
        "children": [
            {"type": "SEQ_SCAN", "est": 1_000, "act": 1_001, "scanned": 1_200, "children": []},
            {"type": "SEQ_SCAN", "est": 20, "act": 21, "scanned": 22, "children": []},
        ],
    }

    assert plan_work(plan, "est") == {
        "scan": 1_020.0,
        "out": 30.0,
        "build": 20.0,
        "probe": 1_000.0,
        "peak": 1_000.0,
        "joins": 1.0,
    }
    assert plan_work(plan, "act")["scan"] == 1_222.0


def test_hash_join_work_tracks_costly_build_sides():
    node = lambda card: SimpleNamespace(card=card)
    joins = [
        SimpleNamespace(left=node(20), right=node(131_072), card=500),
        SimpleNamespace(left=node(BIG_BUILD_ROWS + 1), right=node(BIG_BUILD_ROWS + 10), card=700),
    ]
    plan = SimpleNamespace(joins=lambda: joins)

    assert hash_join_work(plan) == {
        "build": 131_093.0,
        "probe": 262_154.0,
        "out": 1_200.0,
        "out_big_build": 700.0,
        "peak": 700.0,
        "joins": 2.0,
    }
