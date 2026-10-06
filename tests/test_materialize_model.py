from __future__ import annotations

import pytest

from kumosql.joinorder.bench.materialize import (
    Measurements,
    View,
    Workload,
    _queries_for_stage,
    is_held_out,
    measure_baselines,
    measure_view,
)
from kumosql.joinorder.bench.materialize_model import evaluate_pairs, make_pairs
from kumosql.joinorder.runtime_model import Pair


class _WorkFeatures:
    @staticmethod
    def before(query: str) -> dict[str, float]:
        n = float(query[1:])
        return {"scan": n + 1.0, "join": n + 2.0, "query": 1.0}


def _pair(query: str, group: str, before: float, after: float, n: float) -> Pair:
    return Pair(
        query,
        group,
        before,
        after,
        {"k_scan": n + 1.0, "k_join": n + 2.0, "k_query": 1.0},
        {"k_scan": n + 2.0, "k_join": n + 3.0, "k_query": 1.0},
    )


def test_measurement_pairs_obey_the_stable_hash_split():
    names = [f"q{i:03d}" for i in range(100)]
    dev = next(q for q in names if not is_held_out(q))
    held_out = next(q for q in names if is_held_out(q))
    work = Workload({dev: "SELECT 1", held_out: "SELECT 1"}, {})
    view = View("v0", "SELECT 1", [], [], 1, [dev])
    baselines = {q: {"time": 10.0} for q in (dev, held_out)}
    records = {
        "v0": {
            "readers": {
                q: {"time": 5.0, "wrong": False, "same": True, "censored": False}
                for q in (dev, held_out)
            }
        }
    }
    measurements = Measurements(work, [view], {}, baselines, records)

    assert [p["query"] for p in measurements.pairs(held_out=False)] == [dev]
    assert [p["query"] for p in measurements.pairs(held_out=True)] == [held_out]


def test_workflow_stages_default_to_development_and_guard_held_out_queries():
    names = [f"q{i:03d}" for i in range(100)]
    dev = next(q for q in names if not is_held_out(q))
    held_out = next(q for q in names if is_held_out(q))
    work = Workload({dev: "SELECT 1", held_out: "SELECT 1"}, {})

    assert _queries_for_stage(work, None, allow_held_out=False, stage="test") == {dev}
    with pytest.raises(ValueError, match="only after freezing development choices"):
        _queries_for_stage(work, [held_out], allow_held_out=False, stage="test")
    assert _queries_for_stage(work, [held_out], allow_held_out=True, stage="test") == {held_out}


def test_baseline_and_reader_measurements_require_held_out_release():
    names = [f"q{i:03d}" for i in range(100)]
    held_out = next(q for q in names if is_held_out(q))
    work = Workload({held_out: "SELECT 1"}, {})
    view = View("v0", "SELECT 1", [], [], 1, [])

    with pytest.raises(ValueError, match="only after freezing development choices"):
        measure_baselines(None, work, names=[held_out])
    with pytest.raises(ValueError, match="only after freezing development choices"):
        measure_view(None, view, {held_out: "SELECT 1"}, {}, work)


def test_frozen_evaluation_fits_only_on_disjoint_development_queries():
    training = [_pair(f"d{i}", f"v{i}", 20.0 + i, 10.0 + i, float(i)) for i in range(5)]
    scored = [_pair("h9", "v0", 30.0, 20.0, 9.0)]
    baselines = {p.query: p.before for p in training}

    scores = evaluate_pairs(
        scored,
        _WorkFeatures(),
        baselines,
        training_pairs=training,
        resamples=0,
        models=("ratio_kumosql",),
    )

    assert scores["training"] == {"mode": "development_fit", "pairs": 5, "queries": 5}
    assert "ratio_kumosql" in scores


def test_frozen_evaluation_rejects_development_query_overlap():
    pair = _pair("d0", "v0", 20.0, 10.0, 0.0)

    with pytest.raises(ValueError, match="queries overlap"):
        evaluate_pairs(
            [pair],
            _WorkFeatures(),
            {"d0": 20.0},
            training_pairs=[pair],
            resamples=0,
            models=("ratio_kumosql",),
        )


def test_evaluation_requires_an_explicit_training_policy():
    pair = _pair("h9", "v0", 30.0, 20.0, 9.0)

    with pytest.raises(ValueError, match="pass development training_pairs"):
        evaluate_pairs(
            [pair],
            _WorkFeatures(),
            {},
            training_pairs=None,
            resamples=0,
            models=("ratio_kumosql",),
        )


def test_wrong_result_pairs_are_rejected_before_model_fitting():
    with pytest.raises(ValueError, match="wrong-result reader pairs"):
        make_pairs([{"wrong": True}], object())
